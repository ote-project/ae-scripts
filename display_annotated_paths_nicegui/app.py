#!/usr/bin/env python3
import argparse
import asyncio
import concurrent.futures
import html
import json
import queue
import shutil
import tempfile
import uuid
import os
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, TypedDict, Literal

import duckdb
import pandas as pd
import sqlparse
from nicegui import ui

# ------------------ Data & Indexing ------------------
INPUT_FILE_GLOB_PATTERN = "paths-with-conds-*.json.gz"
EVENT_SHARDS_DIRNAME = 'event_shards'

def _write_events_shard(json_file: str, out_dir: str) -> str:
    """Worker: read one JSON(.gz) file, transform to event rows, and write a Parquet shard.
    Returns the output shard path.
    """
    # Local import of duckdb to keep worker minimal
    import duckdb as _dd
    import os
    # Deterministic shard name from source file
    base = os.path.basename(json_file)
    name, _ = os.path.splitext(base)  # .gz or .json.gz -> leave trailing .gz trimmed
    if name.endswith('.json'):
        name = name[:-5]
    shard_path = os.path.join(out_dir, f"{name}.parquet")

    con = _dd.connect()
    # Keep worker-side parallelism available; rely on DuckDB default thread setting
    con.execute("PRAGMA preserve_insertion_order=false;")
    # Materialize the transformed rows directly to Parquet
    dest = shard_path.replace("'", "''")
    con.execute(
        f"""
        COPY (
            WITH r AS (
                SELECT * FROM read_json(?, records=true, filename=true)
            ),
            exploded AS (
                SELECT
                    r.runId::BIGINT  AS runId,
                    r.filename::TEXT AS file,
                    i::INTEGER       AS event_idx,
                    json(list_extract(r.aes, i+1)) AS record
                FROM r, range(array_length(r.aes)) AS idx(i)
            )
            SELECT
                runId,
                file,
                event_idx,
                json_extract(record, '$.elem')       AS elem,
                json_extract_string(elem, '$.$type') AS type,
                json_extract_string(record, '$.vacuousness')::ENUM ('Vacuous', 'NonVacuous') AS vacuousness
            FROM exploded
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """,
        [json_file],
    )
    con.close()
    return os.path.abspath(shard_path)


def _list_input_files(data_dir: Path | str) -> List[str]:
    base = Path(data_dir).expanduser()
    return sorted([str(p) for p in base.glob(INPUT_FILE_GLOB_PATTERN)])


def build_full_index(
    data_dir: Path,
    index_path: Path,
    threads: int,
    memory_limit: str,
    progress_cb: Optional[callable] = None,
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        tmp_index_path = tmpdir_path / "ap_index.duckdb"

        con = duckdb.connect(str(tmp_index_path))
        con.execute("PRAGMA threads=?", [int(threads)])
        con.execute("PRAGMA preserve_insertion_order=?", [False])
        con.execute("PRAGMA memory_limit=?", [memory_limit])
        con.execute("PRAGMA temp_directory=?", [tmpdir])

        # ---------- MAP: JSON(.gz) -> Parquet shards (in parallel) ----------
        files = _list_input_files(data_dir)
        total = len(files)
        if progress_cb is not None:
            progress_cb(0, total, '')

        # Prepare temp shards directory under annotated-paths/
        final_shards_dir = data_dir / EVENT_SHARDS_DIRNAME
        # Build shards inside the function-scoped temporary directory first
        sys_tmp_shards_dir = (tmpdir_path / f".{EVENT_SHARDS_DIRNAME}.build-{uuid.uuid4().hex}")
        sys_tmp_shards_dir.mkdir(parents=True, exist_ok=True)

        # Fan out per-file workers
        done = 0
        with concurrent.futures.ProcessPoolExecutor() as pool:
            futs = [pool.submit(_write_events_shard, f, str(sys_tmp_shards_dir)) for f in files]
            for fut in concurrent.futures.as_completed(futs):
                # Propagate errors early if any
                shard_path = fut.result()
                done += 1
                if progress_cb is not None:
                    progress_cb(done, total, shard_path)

        # Atomically publish shards directory
        # First, move from system temp into a sibling temp inside data_dir (same filesystem as final)
        publish_tmp_dir = data_dir / f".{EVENT_SHARDS_DIRNAME}.publish-{uuid.uuid4().hex}"
        publish_tmp_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sys_tmp_shards_dir), str(publish_tmp_dir))

        backup_dir = None
        if final_shards_dir.exists():
            backup_dir = data_dir / f".{EVENT_SHARDS_DIRNAME}.old-{uuid.uuid4().hex}"
            final_shards_dir.rename(backup_dir)
        # Now the rename is within the same filesystem and therefore atomic
        publish_tmp_dir.rename(final_shards_dir)
        if backup_dir is not None:
            shutil.rmtree(backup_dir, ignore_errors=True)

        # ---------- REDUCE: create tiny DuckDB with VIEWS over Parquet ----------
        # Recreate the temporary index db and define views only.
        if progress_cb is not None:
            progress_cb(total, total, 'reducing: building views…')

        con.execute("DROP VIEW IF EXISTS events;")
        con.execute("DROP VIEW IF EXISTS traces;")
        con.execute("DROP VIEW IF EXISTS queries;")

        shards_glob = str((final_shards_dir / '*.parquet').as_posix())
        # Events view directly over parquet shards
        con.execute(
            f"""
            CREATE VIEW events AS
            SELECT * FROM parquet_scan('{shards_glob}');
            """
        )

        # Traces aggregated from events view
        con.execute(
            """
            CREATE VIEW traces AS
            SELECT runId,
                   any_value(file) AS file,
                   count(*) AS n_events,
                   count(*) FILTER (WHERE type = 'SqlQueryDecl') AS n_sql,
                   count(*) FILTER (WHERE type = 'PathConditionAtom') AS n_conds
            FROM events
            GROUP BY runId;
            """
        )

        # Queries view extracted from elem JSON
        con.execute(
            """
            CREATE VIEW queries AS
            SELECT runId,
                   json_extract(elem, '$.qIdx.value')::INTEGER AS qIdx,
                   lower(json_extract_string(elem, '$.query')) AS query_lc
            FROM events WHERE type = 'SqlQueryDecl';
            """
        )

        con.close() # Ensure all data is flushed and connection is closed before replace
        tmp_index_path.replace(index_path) # Atomically move into place (overwriting any existing index)


@dataclass
class DuckDBConfig:
    threads: int = 4
    memory_limit: str = '8GB'


@dataclass
class AppState:
    sql_sub: str = ''
    min_sql: int = 0
    min_conds: int = 0
    show_stacktraces: bool = False
    duckdb: DuckDBConfig = field(default_factory=DuckDBConfig)
    run_id: Optional[int] = None


# ------------------ Formatting helpers ------------------
def _vac_badge(v: str) -> ui.badge:
    match v:
        case 'Vacuous':
            text = 'Vacuous'
            color = 'grey'
        case 'NonVacuous':
            text = 'NonVacu'
            color = 'primary'
        case _:
            raise ValueError(f"Unknown vacuousness: {v}")

    return ui.badge(text, color=color).props('outline')


def _q_badge(qi: int) -> ui.badge:
    """Standard Q-badge: white text on purple background, dense."""
    return ui.badge(f'Q{int(qi)}', color='purple').props('text-color=white dense')

# ------------------ Fragment model & renderers (for QRV tooltips) ------------------

class Frag(TypedDict, total=False):
    kind: Literal['text', 'qrv']
    text: str
    q: int  # only for kind == 'qrv'


def term_to_frags(term) -> List[Frag]:
    """Return a flat list of fragments for a term; QRVs are marked to attach tooltips."""
    if term == "ConstTrue":
        return [{'kind': 'text', 'text': 'true'}]
    if term == "ConstFalse":
        return [{'kind': 'text', 'text': 'false'}]

    if not isinstance(term, dict):
        raise ValueError(f"Expected term to be a dict, got: {term}")

    match term.get("$type"):
        case "ConstString":
            return [{'kind': 'text', 'text': f"\"{term['value']}\""}]
        case "ConstLong":
            return [{'kind': 'text', 'text': str(term['value'])}]
        case "DeclaredVar":
            return [{'kind': 'text', 'text': term["name"]}]
        case "QueryResVar":
            q = term["qIdx"]["value"]; r = term["rowIdx"]["value"]; c = term["colIdx"]["value"]
            rep = f"Q{q}R{r}C{c}"
            if (name := term.get("colName")) is not None:
                rep = f"{rep}[{name}]"
            return [{'kind': 'qrv', 'text': rep, 'q': q}]
        case "UnaryOp":
            op = term["op"]
            return [{'kind': 'text', 'text': f'{op}('}] + term_to_frags(term['operand']) + [{'kind': 'text', 'text': ')'}]
        case "BinaryOp":
            fr_l = term_to_frags(term.get('lhs'))
            fr_r = term_to_frags(term.get('rhs'))
            if term['op'] == 'Eq':
                return fr_l + [{'kind': 'text', 'text': ' = '}] + fr_r
            else:
                return ([{'kind': 'text', 'text': f"{term.get('op')}("}] +
                        fr_l + [{'kind': 'text', 'text': ', '}] +
                        fr_r + [{'kind': 'text', 'text': ')'}])
        case "Call":
            fn_frag = {'kind': 'text', 'text': term['func']}
            # Flatten args with comma separators: func(a, b, c)
            flat_args: List[Frag] = []
            for i, arg in enumerate(term['args']):
                if i > 0:
                    flat_args.append({'kind': 'text', 'text': ', '})
                flat_args.extend(term_to_frags(arg))
            return ([fn_frag, {'kind': 'text', 'text': '('}] +
                    flat_args +
                    [{'kind': 'text', 'text': ')'}])

        # fallback for unknown types
    return [{'kind': 'text', 'text': str(term)}]

def render_frags(frags: List[Frag], *, pretty_by_qi: Dict[int, str], mono: bool = True) -> None:
    """Render fragments into inline spans; attach a rich tooltip to each QRV fragment.

    For QueryResVar fragments, render as an in-page link to the corresponding
    SqlQueryDecl card (id="q-{qi}") while preserving the existing tooltip.
    """
    for f in frags:
        if f.get('kind') == 'qrv':
            qi = int(f['q'])
            with ui.element():
                # Clickable link to the query card; keep code-like styling
                ui.link(f.get('text', ''), target=f'#q-{qi}', new_tab=False).classes('font-mono text-sm')
                # Rich tooltip with the referenced query
                with ui.tooltip().classes('bg-white text-black border border-grey-5 shadow-md p-0'):
                    with ui.row().classes('items-start gap-2 p-2'):
                        _q_badge(qi)
                        ui.code(pretty_by_qi[qi], language='sql').classes('bg-white text-black max-h-72 overflow-auto m-0')
        else:
            # Plain text fragment
            ui.html(f'<span class="font-mono text-sm">{html.escape(f.get("text", ""))}</span>')


# ------------------ UI & Behavior ------------------


class App:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.data_dir = run_dir / 'annotated-paths'
        self.index_path = self.data_dir / 'ap_index.duckdb'

        self.state = AppState()

        # UI elements we'll update
        self.traces_table = None
        self.run_id_input = None
        self.timeline_container = None
        self.files_found_label = None
        self.index_status_container = None

        self._build_ui()
        self._refresh_files_found()
        self._refresh_index_status()
        self.refresh_traces()

    # ------------- Path display helpers -------------

    @staticmethod
    def shorten_path_for_display(file_path: object, data_dir: Path) -> str:
        """Return a path display relative to the given data directory.

        Accepts any object that can be cast to str; falls back gracefully if
        relative computation fails.
        """
        file_str = str(file_path)
        if not file_str:
            return ""

        file_path = Path(file_str).resolve()
        base_path = data_dir.resolve()

        try:
            return str(file_path.relative_to(base_path))
        except ValueError:
            return file_str

    # ------------- Data helpers -------------

    def _connect_index(self) -> Optional[duckdb.DuckDBPyConnection]:
        if not self.index_path.exists():
            return None
        con = duckdb.connect(str(self.index_path))
        cfg = self.state.duckdb
        con.execute("PRAGMA threads=?", [int(cfg.threads)])
        con.execute("PRAGMA preserve_insertion_order=?", [False])
        con.execute("PRAGMA memory_limit=?", [str(cfg.memory_limit)])
        return con

    # ------------- UI construction -------------

    def _build_ui(self) -> None:
        # Top-level drawer (must not be nested under header)
        self._drawer = ui.left_drawer(value=True, fixed=True).classes('bg-grey-2')

        with self._drawer:
            with ui.column().classes('px-3 py-2 gap-2'):
                ui.label('Filters').classes('text-subtitle2')

                self.files_found_label = ui.label('')

                ui.separator()
                ui.label('Index').classes('text-subtitle2')

                async def on_build_click():
                    # Progress dialog
                    with ui.dialog() as dlg, ui.card().classes('min-w-[500px]'):
                        with ui.column().classes('gap-4 p-2'):
                            ui.label('Building Index').classes('text-h6 text-center')

                            with ui.row().classes('items-center justify-center gap-8'):
                                ui.spinner(size='lg')

                                with ui.column().classes('gap-2'):
                                    # Show the number of CPU cores used by the worker pool
                                    cores = max(1, os.cpu_count() or 1)
                                    with ui.row().classes('items-center gap-2'):
                                        ui.icon('memory').classes('text-primary')
                                        ui.label(f'Cores: {cores}').classes('text-body2')
                                    lp = ui.linear_progress(value=0.0).classes('w-full')
                                    prog_label = ui.label('Waiting…').classes('text-caption text-grey')

                    async def _run_build():
                        q: queue.SimpleQueue[tuple[int, int, str]] = queue.SimpleQueue()

                        def _progress_cb(done: int, total: int, fname: str) -> None:
                            try:
                                q.put((done, total, fname))
                            except Exception:
                                pass

                        # Periodically drain progress updates from the worker thread
                        def _drain_queue():
                            while True:
                                try:
                                    done, total, fname = q.get_nowait()
                                except queue.Empty:
                                    break
                                frac = (done / total) if total else 0.0
                                lp.value = frac
                                if fname:
                                    disp = App.shorten_path_for_display(fname, self.data_dir)
                                    prog_label.text = f"{done}/{total}  {disp}"
                                else:
                                    prog_label.text = f"{done}/{total}"
                                lp.update()
                                prog_label.update()
                        timer = ui.timer(0.2, _drain_queue)

                        dlg.open()
                        # periodic UI updates while building in thread
                        try:
                            await asyncio.to_thread(
                                build_full_index,
                                data_dir=self.data_dir,
                                index_path=self.index_path,
                                threads=int(self.state.duckdb.threads),
                                memory_limit=str(self.state.duckdb.memory_limit) or 'system',
                                progress_cb=_progress_cb,
                            )
                            ui.notify('Index build complete', color='positive')
                        except Exception as e:  # pragma: no cover
                            ui.notify(f'Index build failed: {e}', color='negative', close_button=True)
                        finally:
                            dlg.close()
                            timer.cancel()
                            self._refresh_index_status()
                            self.refresh_traces()

                    await _run_build()

                ui.button('Build/Refresh Full Index', on_click=on_build_click).props('color=primary')

                # index path is always derived from data_dir; no input needed

                ui.separator()
                sql_input = ui.input('SQL contains', value=self.state.sql_sub)
                sql_input.on('change', lambda e: self._on_filter_change(sql=sql_input.value or ''))

                min_sql_input = ui.number('Min #SQL', value=self.state.min_sql, format='%.0f').props('dense')
                min_sql_input.on('change', lambda e: self._on_filter_change(min_sql=int(min_sql_input.value or 0)))

                min_conds_input = ui.number('Min #Conds', value=self.state.min_conds, format='%.0f').props('dense')
                min_conds_input.on('change', lambda e: self._on_filter_change(min_conds=int(min_conds_input.value or 0)))

                ui.checkbox('Show stacktraces', value=self.state.show_stacktraces,
                            on_change=lambda e: self._on_filter_change(show_stacktraces=bool(e.value)))

                with ui.expansion('Advanced (DuckDB)', icon='tune'):
                    thr = ui.number('threads', value=self.state.duckdb.threads, min=1, max=64, format='%.0f')
                    thr.on('change', lambda e: self._on_duckdb_change(threads=int(thr.value or 1)))
                    ml = ui.input('memory_limit', value=self.state.duckdb.memory_limit)
                    ml.on('change', lambda e: self._on_duckdb_change(memory_limit=str(ml.value or '8GB')))

        # Header (separate top-level layout element)
        with ui.header().classes('items-center justify-between'):
            ui.button(on_click=self._drawer.toggle, icon='menu').props('flat round')
            ui.label('Annotated Paths Browser').classes('text-h6')
            ui.space()

        # Main content
        with ui.row().classes('px-4 py-2 gap-4'):
            with ui.column().classes('w-full gap-3'):
                # Index status
                self.index_status_container = ui.row().classes('gap-6 items-center')

                ui.separator()
                ui.label('Traces').classes('text-h6')
                self.traces_table = ui.table(columns=[
                    {'name': 'runId', 'label': 'runId', 'field': 'runId'},
                    {'name': 'file', 'label': 'file', 'field': 'file'},
                    {'name': 'n_events', 'label': 'events', 'field': 'n_events'},
                    {'name': 'n_sql', 'label': 'sql queries', 'field': 'n_sql'},
                    {'name': 'n_conds', 'label': 'conditions', 'field': 'n_conds'},
                ], rows=[], row_key='runId').props('dense flat bordered wrap-cells')
                def _on_row_click(e):
                    try:
                        row = (e.args or {}).get('row') if hasattr(e, 'args') else None
                        rid = row.get('runId') if isinstance(row, dict) else None
                        if rid is not None:
                            self.run_id_input.value = int(rid)
                            self.run_id_input.update()
                            self._on_run_change(int(rid))
                    except Exception:
                        pass
                self.traces_table.on('rowClick', _on_row_click)

                ui.separator()
                ui.label('Run Detail').classes('text-h6')
                with ui.row().classes('items-center gap-2'):
                    # Use precision=0 for UX and normalize value to int in callback
                    self.run_id_input = ui.number(
                        "Run ID",
                        value=0,
                        precision=0,
                        on_change=lambda e: self._on_run_change(e.value),
                    )

                # Timeline container (Raw tab removed)
                self.timeline_container = ui.column().classes('gap-2')

    # ------------- Callbacks -------------

    def _safe_parse_int(self, v) -> Optional[int]:
        try:
            return int(v)
        except Exception:
            try:
                return int(float(v))
            except Exception:
                return None

    def _on_filter_change(self, **kwargs) -> None:
        if 'sql' in kwargs:
            self.state.sql_sub = kwargs['sql']
        if 'min_sql' in kwargs:
            self.state.min_sql = kwargs['min_sql']
        if 'min_conds' in kwargs:
            self.state.min_conds = kwargs['min_conds']
        if 'show_stacktraces' in kwargs:
            self.state.show_stacktraces = kwargs['show_stacktraces']
        self.refresh_traces()

    def _on_duckdb_change(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self.state.duckdb, k, v)
        self._refresh_index_status()
        self.refresh_traces()

    def _on_run_change(self, run_id: int) -> None:
        # Normalize run_id to integer to avoid values like 1.0
        rid = self._safe_parse_int(run_id)
        if rid is None:
            rid = self.state.run_id if self.state.run_id is not None else 0
        self.state.run_id = int(rid)
        self._render_run_detail()

    # ------------- UI refreshers -------------

    def _refresh_files_found(self) -> None:
        if not self.files_found_label:
            return
        n = len(_list_input_files(self.data_dir))
        self.files_found_label.text = f"Found {n} data files"
        self.files_found_label.update()

    def _refresh_index_status(self) -> None:
        if self.index_status_container is None:
            return
        self.index_status_container.clear()
        with self.index_status_container:
            if duckdb is None:
                ui.badge('duckdb not installed').props('color=negative outline')
                return
            con = self._connect_index()
            if not con:
                ui.badge('Index not found').props('color=warning outline')
                return
            try:
                n_traces = con.execute("SELECT count(*) FROM traces").fetchone()[0]
                row = con.execute("SELECT coalesce(sum(n_events),0), coalesce(sum(n_sql),0), coalesce(sum(n_conds),0) FROM traces").fetchone()
                n_events, n_sql, n_conds = int(row[0]), int(row[1]), int(row[2])
            except Exception:
                n_traces = n_events = n_sql = n_conds = 0
            finally:
                con.close()

            for label, val in [('traces', n_traces), ('events', n_events), ('sql queries', n_sql), ('conditions', n_conds)]:
                with ui.card().classes('py-2 px-4'):
                    ui.label(label).classes('text-caption text-grey')
                    ui.label(str(val)).classes('text-h6')

            with suppress(FileNotFoundError):
                ip = self.index_path
                size_mb = ip.stat().st_size / (1024 * 1024)
                ui.label(f"Index: {str(ip)} ({size_mb:.1f} MB)").classes('text-caption')

    def refresh_traces(self) -> None:
        if self.traces_table is None:
            return
        con = self._connect_index()
        if not con:
            self.traces_table.rows = []
            self.traces_table.update()
            return

        where = ["1=1"]
        params: List[object] = []
        if self.state.sql_sub:
            where.append("EXISTS (SELECT 1 FROM queries q WHERE q.runId=t.runId AND q.query_lc LIKE ?)")
            params.append(f"%{self.state.sql_sub.lower()}%")
        if self.state.min_sql:
            where.append("n_sql >= ?")
            params.append(self.state.min_sql)
        if self.state.min_conds:
            where.append("n_conds >= ?")
            params.append(self.state.min_conds)

        base_sql = f"""
          SELECT runId, file, n_events, n_sql, n_conds
          FROM traces t
          WHERE {' AND '.join(where)}
          ORDER BY n_events DESC
          LIMIT 10
        """
        try:
            df = con.execute(base_sql, params).fetchdf()
            # Shorten file paths to be relative to the selected data directory for readability
            if df is not None and not df.empty and 'file' in df.columns:
                df['file'] = df['file'].map(lambda p: App.shorten_path_for_display(p, self.data_dir))
        except Exception:
            df = pd.DataFrame(columns=['runId', 'file', 'n_events', 'n_sql', 'n_conds'])
        finally:
            con.close()

        rows = df.to_dict('records') if not df.empty else []
        self.traces_table.rows = rows
        self.traces_table.update()

        # Default run id
        default_run_id = int(df['runId'].iloc[0]) if not df.empty else 0
        if self.state.run_id is None:
            self.state.run_id = default_run_id
            self.run_id_input.value = default_run_id
            self.run_id_input.update()
        self._render_run_detail()

    def _render_run_detail(self) -> None:
        # Clear containers
        if self.timeline_container:
            self.timeline_container.clear()

        run_id = self.state.run_id if self.state.run_id is not None else 0

        con = self._connect_index()
        if not con:
            with self.timeline_container:
                ui.label("Index not found. Build/refresh index first.")
            return
        try:
            summary = con.execute(
                "SELECT runId, file, n_events, n_sql, n_conds FROM traces WHERE runId=?",
                [run_id],
            ).fetchdf()
            ev_df = con.execute(
                "SELECT * FROM events WHERE runId = ? ORDER BY event_idx",
                [run_id],
            ).fetchdf()
        finally:
            con.close()

        ev_df["elem"] = ev_df["elem"].apply(json.loads)
        # Header metrics
        self._render_run_summary(summary, run_id)
        self._render_events(ev_df)

    def _render_run_summary(self, summary, run_id: int) -> None:
        with self.timeline_container:
            if summary is not None and not summary.empty:
                s = summary.iloc[0]
                with ui.card().classes('w-full'):
                    ui.label(f"Run {run_id}").classes('text-subtitle1')
                    # Show file path relative to the selected data directory for brevity
                    file_disp = self.shorten_path_for_display(s.get('file', ''), self.data_dir)
                    ui.label(f"file: {file_disp}").classes('text-caption')
                    with ui.row().classes('gap-6'):
                        for label, val in [('events', int(s['n_events'])), ('sql queries', int(s['n_sql'])), ('conditions', int(s['n_conds']))]:
                            with ui.card().classes('py-1 px-3'):
                                ui.label(label).classes('text-caption text-grey')
                                ui.label(str(val)).classes('text-body1')
            else:
                ui.label(f"Run {int(run_id)} not found in index").classes('text-negative')

    def _render_stacktrace_if_enabled(self, elem: dict) -> None:
        """Render stacktrace expansion if enabled and stacktrace is present."""
        if self.state.show_stacktraces and (stacktrace := elem.get('stacktrace')):
            with ui.expansion('Stacktrace', icon='stacked_bar_chart'):
                ui.code(stacktrace, language='text')

    def _render_events(self, ev_df) -> None:
        # Ensure all event UI is rendered inside the timeline container to avoid leaking
        # into the current ambient UI context (e.g., the sidebar) from callbacks.
        with self.timeline_container:
            if ev_df is None or ev_df.empty:
                ui.label('(no events)')
                return

            # Precompute qi -> SQL for tooltips (pretty and raw)
            queries_by_qi: Dict[int, str] = {}
            for _, _row in ev_df.iterrows():
                if _row['type'] == 'SqlQueryDecl':
                    elem = _row['elem']
                    _qi = elem['qIdx']['value']
                    queries_by_qi[_qi] = elem['query']
            pretty_by_qi: Dict[int, str] = {
                qi: sqlparse.format(q, reindent=True, keyword_case='upper')
                for qi, q in queries_by_qi.items()
            }

            row_counters: Counter[int] = Counter()
            current_query_card = None

            for _, r in ev_df.iterrows():
                elem = r['elem']
                match elem['$type']:
                    case 'SqlQueryDecl':
                        # Start a new card for a query declaration
                        qi = elem['qIdx']['value']
                        if current_query_card is not None:
                            raise ValueError(f"Unexpected nested SqlQueryDecl for Q{qi}")
                        with (current_query_card := ui.card().props(f'id=q-{qi}').classes('w-full')):
                            # 3-column grid: [index] [Q badge] [content]. Parameters begin under the badge (col 2).
                            one_line = ' '.join(elem['query'].splitlines())
                            is_short = len(one_line) <= 120
                            grid_row_align = 'items-center' if is_short else 'items-start'
                            with ui.grid().classes(f'grid-cols-[auto,auto,1fr] gap-x-2 gap-y-1 {grid_row_align} w-full'):
                                # Row 1, Col 1: index
                                ui.label(f"[{r['event_idx']}]").classes('text-caption text-grey' + (' self-center' if is_short else ''))
                                # Row 1, Col 2: Q badge
                                _q_badge(qi).classes('self-center' if is_short else '')
                                # Row 1, Col 3: content (single-line or multi-line)
                                if is_short:
                                    ui.code(one_line, language='sql').classes('m-0 p-0 whitespace-nowrap min-w-0 self-center')
                                else:
                                    pretty = sqlparse.format(elem['query'], reindent=True, keyword_case='upper')
                                    ui.code(pretty, language='sql').classes('mt-0 min-w-0')

                                # Row 2: Parameters start under the badge (col 2), spanning cols 2-3
                                params = elem['params']
                                with ui.row().classes('items-center gap-2 flex-wrap col-start-2 col-span-2'):
                                    ui.label('Parameters:').classes('text-caption')
                                    if params:
                                        for i, term in enumerate(params, 1):
                                            frags = term_to_frags(term)
                                            with ui.row().classes('items-center gap-1 px-2 py-[2px] rounded border border-grey-5'):
                                                render_frags(frags, pretty_by_qi=pretty_by_qi)
                                    else:
                                        ui.label('(none)').classes('text-caption text-grey')

                            self._render_stacktrace_if_enabled(elem)
                    case 'SqlQueryResRowDecl':
                        qi = elem['qIdx']['value']
                        if current_query_card is None:
                            raise ValueError(f"SqlQueryResRowDecl for Q{qi} without open SqlQueryDecl")
                        with current_query_card:
                            row_id = row_counters[qi]
                            row_counters[qi] += 1
                            with ui.row().classes('items-center gap-2 pl-6'):
                                ui.label(f"[{int(r['event_idx'])}]").classes('text-caption text-grey')
                                _vac_badge(r['vacuousness'])
                                ui.label(f"Q{qi}R{row_id}").classes('font-mono text-sm')
                    case 'SqlQueryResEnd':
                        qi = elem['qIdx']['value']
                        if current_query_card is None:
                            raise ValueError(f"SqlQueryResEnd for Q{qi} without open SqlQueryDecl")
                        with current_query_card:
                            with ui.row().classes('items-center gap-2 pl-6'):
                                ui.label(f"[{int(r['event_idx'])}]").classes('text-caption text-grey')
                                _vac_badge(r['vacuousness'])
                                ui.label("(End)")
                        current_query_card = None
                    case 'PathConditionAtom':
                        if current_query_card is not None:
                            raise ValueError("PathConditionAtom inside SqlQueryDecl")
                        with ui.card().classes('w-full'):
                            with ui.row().classes('items-center gap-2'):
                                ui.label(f"[{int(r['event_idx'])}]").classes('text-caption text-grey')
                                _vac_badge(r.get('vacuousness'))
                                if not elem['outcome']:
                                    ui.badge('not', color='orange').props('text-color=white dense')
                                frags = term_to_frags(elem["cond"])
                                with ui.row().classes('items-center gap-1 flex-wrap'):
                                    render_frags(frags, pretty_by_qi=pretty_by_qi)

                            self._render_stacktrace_if_enabled(elem)
                    case _:
                        raise ValueError(f"Unknown event type: {r['type']}")


def _run_dir(s: str) -> Path:
    p = Path(s).expanduser()
    if not p.is_dir():
        raise argparse.ArgumentTypeError(f"run directory not found: {p}")
    if not (paths_dir := (p / 'annotated-paths')).is_dir():
        raise argparse.ArgumentTypeError(f"run directory does not contain 'annotated-paths' subdirectory: {p}")
    if not any(paths_dir.glob(INPUT_FILE_GLOB_PATTERN)):
        raise argparse.ArgumentTypeError(f"no '{INPUT_FILE_GLOB_PATTERN}' files found in '{paths_dir}'")
    return p.expanduser()


def _port(s: str) -> int:
    try:
        v = int(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError("port must be an integer") from e
    if not (1 <= v <= 65535):
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return v


def parse_args():
    ap = argparse.ArgumentParser(
        description='Browse annotated execution paths with an interactive web interface',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument('run_dir', type=_run_dir, help='Directory containing the output of a run')
    ap.add_argument('--port', type=_port, default=8080, help='Port to run the web server on')
    ap.add_argument('--host', default='127.0.0.1', help='Host/interface to bind the web server to')
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    # Light-weight advisory check for expected files (non-fatal).
    App(run_dir=args.run_dir)
    ui.run(title='Annotated Paths Browser', port=args.port, host=args.host)


if __name__ in {"__main__", "__mp_main__"}:
    main()
