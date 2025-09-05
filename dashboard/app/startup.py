#!/usr/bin/env python3
import argparse
import asyncio
import concurrent.futures
import html
import json
import os
import queue
import re
import shutil
import tempfile
import uuid
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, TypedDict, Literal

import duckdb
import pandas as pd
import sqlparse
from nicegui import ui

from .db import APDB


class Verdict(Enum):
    RELEVANT = 'Relevant'
    IRRELEVANT = 'Irrelevant'
    UNSURE = 'Unsure'
    UNKNOWN = 'Unknown'


# ------------------ Data & Indexing ------------------
# Accept both gzip and zstd compressed JSON inputs
# Example filenames: paths-*.json.gz, paths-*.json.zst
INPUT_FILE_GLOB_PATTERNS = [
    "paths-*.json.zst",
    "paths-*.json.gz",
]
EVENT_SHARDS_DIRNAME = 'event_shards'
# Max characters for rendering a SQL query in one line before switching to pretty block
MAX_ONE_LINE_SQL_LEN = 120

def _write_events_shard(json_file: str, out_dir: str) -> str:
    """Worker: read one JSON(.gz|.zst) file, transform to event rows, and write a Parquet shard.
    Returns the output shard path.
    """
    # Deterministic shard name from source file
    base = os.path.basename(json_file)
    name, _ = os.path.splitext(base)  # .gz/.zst or .json.gz/.json.zst -> leave trailing compressed ext trimmed
    if name.endswith('.json'):
        name = name[:-5]
    shard_path = os.path.join(out_dir, f"{name}.parquet")

    with tempfile.TemporaryDirectory() as tmpdir:
        con = duckdb.connect()
        # Constrain each worker to a single DuckDB thread to avoid per-worker thread blow-up
        con.execute("PRAGMA threads=1;")
        con.execute("PRAGMA temp_directory=?", [tmpdir])
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
                    json_extract_string(record, '$.vacuousness')::ENUM ('Vacuous', 'NonVacuous') AS vacuousness,
                    json_extract_string(record, '$.oracleDigest') AS oracle_digest
                FROM exploded
            ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """,
            [json_file],
        )
        con.close()
    return os.path.abspath(shard_path)


def _list_input_files(data_dir: Path | str) -> List[str]:
    """Return a sorted list of input files.

    Raises ValueError if duplicates exist for the same base name across
    compression extensions (e.g., both .json.gz and .json.zst present).
    """
    base = Path(data_dir).expanduser()

    def _stem_for(p: Path) -> str:
        # Mirror shard naming logic: drop last ext, then drop trailing .json
        name, _ = os.path.splitext(p.name)
        return name[:-5] if name.endswith('.json') else name

    by_key: dict[str, List[Path]] = {}
    for pat in INPUT_FILE_GLOB_PATTERNS:
        for p in base.glob(pat):
            key = _stem_for(p)
            by_key.setdefault(key, []).append(p)

    # Detect duplicates where more than one file maps to the same key
    duplicates = {k: v for k, v in by_key.items() if len(v) > 1}
    if duplicates:
        parts: List[str] = []
        for k, paths in duplicates.items():
            listed = ', '.join(sorted(str(x.name) for x in paths))
            parts.append(f"{k}: [{listed}]")
        details = '; '.join(parts)
        raise ValueError(f"duplicate inputs for base name(s): {details}")

    files = [p for lst in by_key.values() for p in lst]
    return [str(p) for p in sorted(files, key=lambda x: x.name)]


def build_full_index(
    data_dir: Path,
    index_path: Path,
    threads: int,
    memory_limit: str,
    progress_cb: Optional[callable] = None,
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # ---------- MAP: JSON(.gz|.zst) -> Parquet shards (in parallel) ----------
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
        with concurrent.futures.ProcessPoolExecutor(max_workers=threads) as pool:
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

        tmp_index_path = tmpdir_path / "ap_index.duckdb"
        con = duckdb.connect(str(tmp_index_path))
        con.execute("PRAGMA threads=?", [threads])
        con.execute("PRAGMA preserve_insertion_order=?", [False])
        con.execute("PRAGMA memory_limit=?", [memory_limit])
        con.execute("PRAGMA temp_directory=?", [tmpdir])

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
    return ui.badge(f'Q{int(qi)}').props('text-color=white dense')

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
        self.oracle_container = None
        self.files_found_label = None
        self.index_status_container = None
        self.main_tabs = None
        self.tab_runs = None
        self.tab_oracle = None
        self.sidebar_runs = None

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
        cfg = self.state.duckdb
        return APDB.connect(self.index_path, threads=int(cfg.threads), memory_limit=str(cfg.memory_limit))

    # ------------- UI construction -------------

    def _build_ui(self) -> None:
        # Inject CSS and build major layout sections
        self._inject_global_css()
        self._build_drawer_and_sidebar()
        self._build_header()
        self._build_main_panels()
        self._wire_tab_sidebar_visibility()

    # --- UI sub-builders ---
    def _inject_global_css(self) -> None:
        css = (
            '.cm-editor{width:100%}'
            '.cm-scroller{overflow:auto}'
            '.cm-content{min-width:0!important}'
            '.prompt-cm .cm-editor{min-height:18rem}'
            '.stacktrace-cm .cm-editor{min-height:18rem}'
            '.stacktrace-dialog .cm-editor{height:100%}'
            '#oracle-grid{width:100%!important;max-width:none!important}'
            '#oracle-grid .ag-root-wrapper{width:100%!important}'
            '.q-page, .q-page-container{max-width:none!important}'
            'body, .q-app, .q-page-container, .q-page{height:100vh;display:flex;flex-direction:column}'
        )
        ui.add_css(css)

    def _build_drawer_and_sidebar(self) -> None:
        # Top-level drawer (must not be nested under header)
        self._drawer = ui.left_drawer(value=True, fixed=True).classes('bg-grey-2')
        with self._drawer:
            with ui.column().classes('px-3 py-2 gap-2'):
                # Runs-only sidebar container (hidden on Oracle tab)
                self.sidebar_runs = ui.column().classes('gap-2 w-full')
                with self.sidebar_runs:
                    self.files_found_label = ui.label('')
                    self._build_index_controls()
                    self._build_filters()
                    self._build_traces_table()

    def _build_index_controls(self) -> None:
        ui.separator()
        ui.label('Index').classes('text-subtitle2')
        ui.button('Build/Refresh Full Index', on_click=self.on_build_index_click).props('color=primary')
        # Index status lives in the sidebar
        self.index_status_container = ui.card().classes('w-full p-2')

    def _build_filters(self) -> None:
        # Run selector
        ui.separator()
        ui.label('Run').classes('text-subtitle2')
        # Use precision=0 for UX; keep in sync with main run change handler
        self.run_id_input = ui.number('Run ID', value=0, precision=0).props('dense')
        # NiceGUI's generic change event doesn't provide e.value; read from the component instead
        self.run_id_input.on('change', lambda e: self._on_run_change(self.run_id_input.value))

        ui.separator()
        sql_input = ui.input('SQL contains', value=self.state.sql_sub)
        sql_input.on('change', lambda e: self._on_filter_change(sql=sql_input.value or ''))

        min_sql_input = ui.number('Min #SQL', value=self.state.min_sql, format='%.0f').props('dense')
        min_sql_input.on('change', lambda e: self._on_filter_change(min_sql=int(min_sql_input.value or 0)))

        min_conds_input = ui.number('Min #Conds', value=self.state.min_conds, format='%.0f').props('dense')
        min_conds_input.on('change', lambda e: self._on_filter_change(min_conds=int(min_conds_input.value or 0)))

    def _build_traces_table(self) -> None:
        # Traces table
        ui.separator()
        ui.label('Traces').classes('text-subtitle2')
        self.traces_table = ui.table(columns=[
            {'name': 'runId', 'label': 'runId', 'field': 'runId'},
            {'name': 'n_events', 'label': 'events', 'field': 'n_events'},
            {'name': 'n_sql', 'label': 'sql queries', 'field': 'n_sql'},
            {'name': 'n_conds', 'label': 'conditions', 'field': 'n_conds'},
        ], rows=[], row_key='runId').props('dense flat bordered wrap-cells')
        self.traces_table.on('rowClick', self.on_traces_row_click)

        with ui.expansion('Advanced (DuckDB)', icon='tune'):
            thr = ui.number('threads', value=self.state.duckdb.threads, min=1, max=64, format='%.0f')
            thr.on('change', lambda e: self._on_duckdb_change(threads=int(thr.value or 1)))
            ml = ui.input('memory_limit', value=self.state.duckdb.memory_limit)
            ml.on('change', lambda e: self._on_duckdb_change(memory_limit=str(ml.value or '8GB')))

    def _build_header(self) -> None:
        # Header (separate top-level layout element)
        with ui.header().classes('items-center justify-between'):
            ui.button(on_click=self._drawer.toggle, icon='menu').props('flat round')
            # Title with data path underneath
            with ui.column().classes('items-start max-w-[70vw]'):
                _data_path = str(self.run_dir)
                _path_lbl = ui.label(_data_path).classes('text-white opacity-90 font-mono truncate max-w-full')
                with _path_lbl:
                    ui.tooltip(_data_path)
            ui.space()
            # Place the main tabs in the header so they appear above the sidebar
            with ui.tabs() as self.main_tabs:
                self.tab_runs = ui.tab('Runs')
                self.tab_oracle = ui.tab('Oracle')

    def _build_main_panels(self) -> None:
        # Main content with top-level tabs for Runs and Oracle
        with ui.row().classes('px-4 py-2 gap-4 w-full max-w-none items-stretch flex-1').style('width: 100%'):
            with ui.column().classes('w-full gap-3 max-w-none flex-1'):
                # Panels are driven by the tabs in the header
                with ui.tab_panels(self.main_tabs, value=self.tab_runs).classes('w-full flex-1'):
                    with ui.tab_panel(self.tab_runs):
                        # Timeline container for run details
                        self.timeline_container = ui.column().classes('gap-2 w-full flex-1')
                    with ui.tab_panel(self.tab_oracle):
                        # Global Oracle view (independent of runs/index)
                        self.oracle_container = ui.column().classes('w-full flex-1')
                        with self.oracle_container:
                            self._render_oracle_outputs(parent=self.oracle_container)

    def _wire_tab_sidebar_visibility(self) -> None:
        # Initial state: Runs tab -> show sidebar
        self._toggle_sidebar_for_tab(self.tab_runs)
        try:
            self.main_tabs.on_value_change(lambda e: self._toggle_sidebar_for_tab(getattr(e, 'value', None)))
        except Exception:
            pass

    def _toggle_sidebar_for_tab(self, val) -> None:
        is_runs = (val == self.tab_runs) or (getattr(val, 'text', None) == 'Runs') or (val == 'Runs')
        if self.sidebar_runs is None:
            return
        try:
            self.sidebar_runs.visible = bool(is_runs)
            self.sidebar_runs.update()
        except Exception:
            # Fallback for older NiceGUI: flip display style
            if is_runs:
                self.sidebar_runs.style('display:flex')
            else:
                self.sidebar_runs.style('display:none')
        # Also open/close the drawer itself to fully hide the sidebar on Oracle
        try:
            self._drawer.value = bool(is_runs)
            self._drawer.update()
        except Exception:
            pass

    # --- Sidebar callbacks ---
    async def on_build_index_click(self):
        dlg, lp, prog_label = self._create_index_build_dialog()
        q: queue.SimpleQueue[tuple[int, int, str]] = queue.SimpleQueue()
        timer = ui.timer(0.2, lambda: self._drain_build_progress_queue(q, lp, prog_label))

        dlg.open()
        try:
            await asyncio.to_thread(
                build_full_index,
                data_dir=self.data_dir,
                index_path=self.index_path,
                threads=int(self.state.duckdb.threads),
                memory_limit=str(self.state.duckdb.memory_limit) or 'system',
                progress_cb=lambda d, t, f: self._enqueue_build_progress(q, d, t, f),
            )
            ui.notify('Index build complete', color='positive')
        except Exception as e:  # pragma: no cover
            ui.notify(f'Index build failed: {e}', color='negative', close_button=True)
        finally:
            dlg.close()
            timer.cancel()
            self._refresh_index_status()
            self.refresh_traces()

    def _create_index_build_dialog(self):
        # Create and return dialog, progress bar and label
        with ui.dialog() as dlg, ui.card().classes('min-w-[500px]'):
            with ui.column().classes('gap-4 p-2'):
                ui.label('Building Index').classes('text-h6 text-center')

            with ui.row().classes('items-center justify-center gap-8'):
                ui.spinner(size='lg')

                with ui.column().classes('gap-2'):
                    cores = max(1, os.cpu_count() or 1)
                    with ui.row().classes('items-center gap-2'):
                        ui.icon('memory').classes('text-primary')
                        ui.label(f'Cores: {cores}').classes('text-body2')
                    lp = ui.linear_progress(value=0.0).classes('w-full')
                    prog_label = ui.label('Waiting…').classes('text-caption text-grey')
        return dlg, lp, prog_label

    @staticmethod
    def _enqueue_build_progress(q: 'queue.SimpleQueue[tuple[int,int,str]]', done: int, total: int, fname: str) -> None:
        try:
            q.put((done, total, fname))
        except Exception:
            pass

    def _drain_build_progress_queue(self, q: 'queue.SimpleQueue[tuple[int,int,str]]', lp, prog_label) -> None:
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

    def on_traces_row_click(self, e):
        try:
            row = (e.args or {}).get('row') if hasattr(e, 'args') else None
            rid = row.get('runId') if isinstance(row, dict) else None
            if rid is not None:
                self.run_id_input.value = int(rid)
                self.run_id_input.update()
                self._on_run_change(int(rid))
        except Exception:
            pass

    # --- Small helpers ---
    @staticmethod
    def format_sql(text: str) -> str:
        return sqlparse.format(text, reindent=True, keyword_case='upper')

    @staticmethod
    def sql_one_liner(text: str, max_len: int = MAX_ONE_LINE_SQL_LEN) -> tuple[str, bool]:
        one_line = ' '.join((text or '').splitlines())
        return one_line, len(one_line) <= max_len

    @staticmethod
    def compute_pretty_by_qi(ev_df) -> Dict[int, str]:
        if ev_df is None or ev_df.empty:
            return {}
        queries_by_qi: Dict[int, str] = {}
        for _, _row in ev_df.iterrows():
            if _row['type'] == 'SqlQueryDecl':
                elem = _row['elem']
                _qi = elem['qIdx']['value']
                queries_by_qi[_qi] = elem['query']
        return {qi: App.format_sql(q) for qi, q in queries_by_qi.items()}

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
        try:
            n = len(_list_input_files(self.data_dir))
            self.files_found_label.text = f"Found {n} data files"
        except ValueError as e:
            self.files_found_label.text = f"Input error: {e}"
        self.files_found_label.update()

    def _refresh_index_status(self) -> None:
        if self.index_status_container is None:
            return
        self.index_status_container.clear()
        with self.index_status_container:
            ui.label('Index Status').classes('text-caption text-grey-7')

            if duckdb is None:
                with ui.row().classes('items-center gap-2'):
                    ui.icon('error').classes('text-negative')
                    ui.label('DuckDB not installed').classes('text-negative')
                return

            con = self._connect_index()
            if not con:
                with ui.row().classes('items-center gap-2'):
                    ui.icon('warning').classes('text-warning')
                    ui.label('Index not found').classes('text-warning')
                return

            with suppress(FileNotFoundError):
                ip = self.index_path
                # Show path relative to the run directory for conciseness
                try:
                    disp = str(ip.resolve().relative_to(self.run_dir.resolve()))
                except Exception:
                    disp = str(ip)
                ui.label(disp).classes('text-caption text-grey')

            try:
                n_traces, n_events = APDB.get_index_stats(con)
            finally:
                con.close()

            with ui.row().classes('gap-2 flex-wrap'):
                for label, val in [
                    ('traces', n_traces),
                    ('events', n_events),
                ]:
                    with ui.row().classes('items-baseline gap-1 px-2 py-[2px] rounded border border-grey-5 bg-white'):
                        ui.label(label).classes('text-caption text-grey-7')
                        ui.label(str(val)).classes('text-body2 font-medium')

    def refresh_traces(self) -> None:
        if self.traces_table is None:
            return
        con = self._connect_index()
        if not con:
            self.traces_table.rows = []
            self.traces_table.update()
            return

        try:
            df = APDB.fetch_traces(
                con,
                sql_sub=self.state.sql_sub,
                min_sql=int(self.state.min_sql or 0),
                min_conds=int(self.state.min_conds or 0),
                limit=10,
            )
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
            summary = APDB.fetch_summary(con, run_id)
            ev_df = APDB.fetch_events(con, run_id)
        finally:
            con.close()

        ev_df["elem"] = ev_df["elem"].apply(json.loads)
        # Header metrics
        self._render_run_summary(summary, run_id)

        # Run detail content: Trace only (Oracle moved to top-level tab)
        with self.timeline_container:
            with ui.column().classes('gap-2') as trace_panel:
                self._render_events(ev_df, parent=trace_panel)

    def _render_run_summary(self, summary, run_id: int) -> None:
        with self.timeline_container:
            if summary is not None and not summary.empty:
                s = summary.iloc[0]
                # Compact summary card
                with ui.card().classes('w-full p-2'):
                    with ui.row().classes('items-center justify-between w-full'):
                        ui.label(f"Run {run_id}").classes('text-body1 font-medium')
                        # File path relative to selected data directory
                        file_disp = self.shorten_path_for_display(s.get('file', ''), self.data_dir)
                        ui.label(f"file: {file_disp}").classes('text-caption text-grey')
                    with ui.row().classes('gap-2 flex-wrap mt-1'):
                        for label, val in [
                            ('events', int(s['n_events'])),
                            ('sql', int(s['n_sql'])),
                            ('conds', int(s['n_conds'])),
                        ]:
                            with ui.row().classes('items-baseline gap-1 px-2 py-[2px] rounded border border-grey-5 bg-white'):
                                ui.label(label).classes('text-caption text-grey-7')
                                ui.label(str(val)).classes('text-body2 font-medium')
            else:
                ui.label(f"Run {int(run_id)} not found in index").classes('text-negative')

    @staticmethod
    def _render_stacktrace(elem: dict, *, compact: bool = False, pretty_by_qi: Optional[Dict[int, str]] = None) -> None:
        """Render a button that opens a large dialog showing the stacktrace (if present).

        Args:
            elem: Event element dict which may contain 'stacktrace'.
            compact: If True, render a small, subtle inline icon button suitable for inline use.
            pretty_by_qi: Optional mapping of query index to pretty SQL, used to render
                rich QRV tooltips if the element is a PathConditionAtom.
        """
        stack_val = elem.get('stacktrace')
        if not stack_val:
            return

        # Normalize to plain text
        if isinstance(stack_val, list):
            stack_text = '\n'.join(stack_val)
        else:
            stack_text = str(stack_val)

        # Pre-create dialog and a button to open it
        with ui.dialog() as dlg:
            # Prevent refocus on close to avoid page scroll jumps
            dlg.props('maximized no-refocus')
            # Fullscreen card with flex column to pin footer at bottom
            with ui.card().classes('w-screen h-screen max-w-none max-h-none p-2 flex flex-col gap-2'):
                with ui.row().classes('items-center justify-between w-full'):
                    ui.label('Stacktrace').classes('text-subtitle1')
                    ui.button(icon='close', on_click=dlg.close).props('flat round dense')
                # If this is a PathConditionAtom, show its condition above the stacktrace
                try:
                    if elem.get('$type') == 'PathConditionAtom' and 'cond' in elem:
                        # Render condition inline with optional QRV tooltips
                        with ui.column().classes('gap-1 w-full'):
                            ui.label('Condition').classes('text-caption text-grey')
                            # Compute fragments; prefix '!' if outcome is False
                            frags = term_to_frags(elem.get('cond'))
                            if elem.get('outcome') is False:
                                frags = [{'kind': 'text', 'text': '!'}] + frags
                            if pretty_by_qi:
                                with ui.row().classes('items-center gap-1 flex-wrap'):
                                    render_frags(frags, pretty_by_qi=pretty_by_qi)
                            else:
                                # Fallback to plain text if we don't have query pretties
                                text = ''.join(f.get('text', '') for f in frags)
                                ui.html(f'<span class="font-mono text-sm">{html.escape(text)}</span>')
                    elif elem.get('$type') == 'SqlQueryDecl' and 'query' in elem:
                        # Show the SQL query above the stacktrace
                        with ui.column().classes('gap-1 w-full'):
                            ui.label('Query').classes('text-caption text-grey')
                            try:
                                qi = int(elem.get('qIdx', {}).get('value'))
                            except Exception:
                                qi = None
                            # If we know qi, show the standard Q badge
                            if qi is not None:
                                with ui.row().classes('items-start gap-2'):
                                    _q_badge(qi)
                                    pretty = (pretty_by_qi or {}).get(qi) or App.format_sql(elem['query'])
                                    ui.code(pretty, language='sql').classes('m-0')
                            else:
                                pretty = App.format_sql(elem['query'])
                                ui.code(pretty, language='sql').classes('m-0')
                except Exception:
                    # Be robust: failure to render condition shouldn't break stacktrace dialog
                    pass
                # Growing content area that the editor will fill
                with ui.element('div').classes('flex-1 min-h-0 w-full'):
                    ui.codemirror(value=stack_text, line_wrapping=True).classes('w-full h-full stacktrace-dialog')
                with ui.row().classes('justify-end w-full'):
                    ui.button('Close', on_click=dlg.close).props('flat')

        if compact:
            btn = ui.button(icon='troubleshoot', on_click=dlg.open).props('flat dense color=primary').classes('rounded-none w-7 h-7 ml-2')
            with btn:
                ui.tooltip('Stacktrace')
        else:
            ui.button(icon='troubleshoot', on_click=dlg.open).props('outline color=primary').classes('w-10 h-10 rounded-none')

    def _render_events(self, ev_df, *, parent=None) -> None:
        """Render the events timeline into the given parent (or the timeline container)."""
        container = parent or self.timeline_container
        with container:
            if ev_df is None or ev_df.empty:
                ui.label('(no events)')
                return

            pretty_by_qi = self.compute_pretty_by_qi(ev_df)
            oracle_records, _ = self._load_oracle_records()
            oracle_by_digest: Dict[str, dict] = {r['key_digest']: r for r in oracle_records}

            ctx = App.QueryContext(
                row_counters=Counter(),
                current_query_card=None,
                current_query_irrelevant_badged=False,
                pretty_by_qi=pretty_by_qi,
                oracle_by_digest=oracle_by_digest,
            )

            handlers = {
                'SqlQueryDecl': self._ev_sql_decl,
                'SqlQueryResRowDecl': self._ev_sql_row,
                'SqlQueryResEnd': self._ev_sql_end,
                'PathConditionAtom': self._ev_path_cond,
            }

            for _, r in ev_df.iterrows():
                elem = r['elem']
                fn = handlers.get(elem['$type'])
                if fn is None:
                    raise ValueError(f"Unknown event type: {r['type']}")
                fn(r, elem, ctx)

    @dataclass
    class QueryContext:
        row_counters: Counter
        current_query_card: Optional[object]
        current_query_irrelevant_badged: bool
        pretty_by_qi: Dict[int, str]
        oracle_by_digest: Dict[str, dict]

    def _ev_sql_decl(self, r, elem, ctx: 'App.QueryContext') -> None:
        qi = elem['qIdx']['value']
        if ctx.current_query_card is not None:
            raise ValueError(f"Unexpected nested SqlQueryDecl for Q{qi}")
        try:
            od = r.get('oracle_digest') if isinstance(r, dict) else r['oracle_digest']
        except Exception:
            od = None
        rec = ctx.oracle_by_digest.get(od) if od else None
        ctx.current_query_irrelevant_badged = bool(rec is not None and rec.get('verdict') == Verdict.IRRELEVANT)
        card = ui.card().props(f'id=q-{qi}').classes('w-full')
        ctx.current_query_card = card
        with card:
            one_line, is_short = App.sql_one_liner(elem['query'])
            grid_row_align = 'items-center' if is_short else 'items-start'
            with ui.grid().classes(f'grid-cols-[auto,auto,1fr] gap-x-2 gap-y-1 {grid_row_align} w-full'):
                ui.label(f"[{r['event_idx']}]").classes('text-caption text-grey' + (' self-center' if is_short else ''))
                with ui.row().classes('items-center gap-2' + (' self-center' if is_short else '')):
                    _q_badge(qi)
                    if rec is not None:
                        badge = self._verdict_badge(rec.get('verdict')).classes('cursor-pointer')
                        with ui.dialog() as dlg:
                            dlg.props('maximized no-refocus')
                            with ui.card().classes('w-screen h-screen max-w-none max-h-none p-2 flex flex-col gap-2 overflow-hidden'):
                                with ui.row().classes('items-center justify-between w-full'):
                                    ui.label(f'Oracle Details for Q{qi}').classes('text-subtitle1')
                                    ui.button(icon='close', on_click=dlg.close).props('flat round dense')
                                with ui.element('div').classes('flex-1 min-h-0 w-full overflow-auto'):
                                    self._render_oracle_record_details_content(rec)
                                with ui.row().classes('justify-end w-full'):
                                    ui.button('Close', on_click=dlg.close).props('flat')
                        try:
                            badge.on('click', dlg.open)
                        except Exception:
                            pass
                if is_short:
                    with ui.row().classes('items-center gap-2 min-w-0 self-center'):
                        ui.code(one_line, language='sql').classes('m-0 p-0 whitespace-nowrap min-w-0 self-center flex-1')
                        self._render_stacktrace(elem, compact=True, pretty_by_qi=ctx.pretty_by_qi)
                else:
                    pretty = App.format_sql(elem['query'])
                    with ui.row().classes('items-start gap-2 min-w-0'):
                        with ui.element('div').classes('flex-1 min-w-0'):
                            ui.code(pretty, language='sql').classes('mt-0 min-w-0')
                        self._render_stacktrace(elem, compact=True, pretty_by_qi=ctx.pretty_by_qi)
                params = elem['params']
                with ui.row().classes('items-center gap-2 flex-wrap col-start-2 col-span-2'):
                    ui.label('Parameters:').classes('text-caption')
                    if params:
                        for _, term in enumerate(params, 1):
                            frags = term_to_frags(term)
                            with ui.row().classes('items-center gap-1 px-2 py-[2px] rounded border border-grey-5'):
                                render_frags(frags, pretty_by_qi=ctx.pretty_by_qi)
                    else:
                        ui.label('(none)').classes('text-caption text-grey')

    def _ev_sql_row(self, r, elem, ctx: 'App.QueryContext') -> None:
        qi = elem['qIdx']['value']
        if ctx.current_query_card is None:
            raise ValueError(f"SqlQueryResRowDecl for Q{qi} without open SqlQueryDecl")
        with ctx.current_query_card:
            row_id = ctx.row_counters[qi]
            ctx.row_counters[qi] += 1
            with ui.row().classes('items-center gap-2 pl-6'):
                ui.label(f"[{int(r['event_idx'])}]").classes('text-caption text-grey')
                if not ctx.current_query_irrelevant_badged:
                    _vac_badge(r['vacuousness'])
                ui.label(f"Q{qi}R{row_id}").classes('font-mono text-sm')

    def _ev_sql_end(self, r, elem, ctx: 'App.QueryContext') -> None:
        qi = elem['qIdx']['value']
        if ctx.current_query_card is None:
            raise ValueError(f"SqlQueryResEnd for Q{qi} without open SqlQueryDecl")
        with ctx.current_query_card:
            with ui.row().classes('items-center gap-2 pl-6'):
                ui.label(f"[{int(r['event_idx'])}]").classes('text-caption text-grey')
                if not ctx.current_query_irrelevant_badged:
                    _vac_badge(r['vacuousness'])
                ui.label("(End)")
        ctx.current_query_card = None
        ctx.current_query_irrelevant_badged = False

    def _ev_path_cond(self, r, elem, ctx: 'App.QueryContext') -> None:
        if ctx.current_query_card is not None:
            raise ValueError("PathConditionAtom inside SqlQueryDecl")
        with ui.card().classes('w-full'):
            with ui.row().classes('items-start gap-2 w-full'):
                ui.label(f"[{int(r['event_idx'])}]").classes('text-caption text-grey')
                _vac_badge(r['vacuousness'])
                frags = term_to_frags(elem["cond"])
                if elem['outcome'] is False:
                    frags = [{'kind': 'text', 'text': '!'}] + frags
                with ui.row().classes('items-center gap-1 flex-wrap flex-1 min-w-0'):
                    render_frags(frags, pretty_by_qi=ctx.pretty_by_qi)
                self._render_stacktrace(elem, compact=True, pretty_by_qi=ctx.pretty_by_qi)

    # ------------------ Terminal-style renderer ------------------
    @staticmethod
    def render_terminal(text: Optional[str], *, wrap: bool = True, max_height: str = '30rem') -> None:
        """Render plain text in a terminal-like block (monospace, preserved whitespace).

        Args:
            text: The text to show; None is treated as empty.
            wrap: If True, long lines wrap; if False, allow horizontal scrolling.
            max_height: Tailwind size for max height (e.g., '18rem', '24rem').
        """
        content = '' if text is None else str(text)
        classes = 'font-mono text-sm bg-grey-1 border border-grey-4 rounded p-2 w-full '
        classes += 'whitespace-pre-wrap break-words ' if wrap else 'whitespace-pre '
        classes += 'overflow-auto'
        # Use inline style for max-height to avoid Tailwind JIT issues with dynamic class names
        safe = html.escape(content)
        ui.html(f'<pre class="{classes}" style="max-height: {max_height};">{safe}</pre>')

    # ------------------ Oracle outputs (query relevance) ------------------
    @staticmethod
    def _preview(text: Optional[str], length: int = 120) -> str:
        if not text:
            return ''
        t = text.replace('\n', ' ')
        return t if len(t) <= length else t[: length - 1] + '…'

    @staticmethod
    def _compute_tokens_used_from_stdout(stdout: Optional[str]) -> Optional[int]:
        if not stdout:
            return None
        m = re.findall(r"tokens used: (\d+)", stdout)
        return int(m[-1]) if m else None

    @staticmethod
    def _compute_verdict(rec: dict) -> Verdict:
        """Map record verdict to a Verdict enum (Relevant, Irrelevant, Unsure, Unknown)."""
        if (v := rec.get('verdict')) is None:
            return Verdict.UNKNOWN

        vl = v.strip().lower()
        if vl in {'yes', 'relevant'}:
            return Verdict.RELEVANT
        if vl in {'no', 'irrelevant'}:
            return Verdict.IRRELEVANT
        if vl == 'unsure':
            return Verdict.UNSURE

        return Verdict.UNKNOWN

    @staticmethod
    def _verdict_badge(verdict: Verdict) -> ui.badge:
        match verdict:
            case Verdict.RELEVANT:
                color, text_color = 'positive', 'white'
            case Verdict.IRRELEVANT:
                color, text_color = 'negative', 'white'
            case Verdict.UNSURE:
                # Yellow background benefits from dark text for contrast
                color, text_color = 'warning', 'black'
            case Verdict.UNKNOWN:
                # Grey background: use dark text for readability
                color, text_color = 'grey', 'black'
        return ui.badge(verdict.value, color=color).props(f'text-color={text_color}')

    def _load_oracle_records(self) -> tuple[List[dict], Optional[Path]]:
        chosen = self.run_dir / 'oracle-logs'
        if not chosen.is_dir():
            return [], None

        # Load both query and conditional oracle outputs
        files = sorted(chosen.glob('codex-*.jsonl'))
        recs: List[dict] = []
        for fp in files:
            with fp.open('r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    recs.append(rec)

        # Normalize
        for i, r in enumerate(recs):
            r['row_idx'] = i
            r['verdict'] = self._compute_verdict(r)
            if 'tokens_used' not in r:
                r['tokens_used'] = self._compute_tokens_used_from_stdout(r.get('stdout'))
            # Subject normalization (query vs conditional)
            # Some datasets use 'condition' for codex-conditional; tolerate both.
            subj = r.get('query') or r.get('conditional') or r.get('condition') or ''
            r['subject'] = subj
            r['subject_preview'] = self._preview(subj)
        return recs, chosen

    # Shared renderer for Oracle record details (used in tab and fullscreen dialog)
    def _render_oracle_record_details_content(self, rec: dict) -> None:
        """Render the Oracle record details content (metadata + sections)."""
        with ui.column().classes('gap-2 w-full min-w-0'):
            self._render_oracle_meta(rec)
            with ui.column().classes('w-full min-w-0 gap-2'):
                self._render_oracle_sql_section(rec)
                self._render_oracle_stacktrace_section(rec)
                self._render_oracle_report_section(rec)
                self._render_oracle_stdio_section(rec)

    def _render_oracle_meta(self, rec: dict) -> None:
        with ui.grid().classes('grid-cols-[auto,2fr,1fr,1fr,1fr,1fr] gap-8 w-full min-w-0'):
            with ui.column():
                ui.label('Row ID').classes('text-caption text-grey')
                ui.label(str(rec.get('row_idx', '—')))
            with ui.column().classes('min-w-0'):
                ui.label('Key digest').classes('text-caption text-grey')
                kd_full = str(rec.get('key_digest', '—'))
                kd_lbl = ui.label(kd_full).classes('font-mono truncate max-w-full')
                with kd_lbl:
                    ui.tooltip(kd_full)
            with ui.column():
                ui.label('Verdict').classes('text-caption text-grey')
                self._verdict_badge(rec.get('verdict'))
            with ui.column():
                ui.label('Tokens used').classes('text-caption text-grey')
                ui.label(str(rec.get('tokens_used', '—')))
            with ui.column():
                ui.label('Duration (s)').classes('text-caption text-grey')
                ui.label(str(rec.get('dur_s', '—')))
            with ui.column():
                ui.label('Exit code').classes('text-caption text-grey')
                ui.label(str(rec.get('exit_code', '—')))

    def _render_oracle_sql_section(self, rec: dict) -> None:
        # Backward-compat alias if called elsewhere; choose section based on available fields.
        self._render_oracle_subject_section(rec)

    def _render_oracle_subject_section(self, rec: dict) -> None:
        oracle_kind = str(rec.get('oracle') or '')
        # Prefer explicit fields; support both 'conditional' and 'condition'
        if oracle_kind == 'codex-conditional' or ('conditional' in rec or 'condition' in rec) and not rec.get('query'):
            subject = rec.get('conditional') or rec.get('condition') or ''
            with ui.expansion('Conditional', icon='rule', value=True).classes('w-full'):
                # Plain text display
                try:
                    ui.code(subject, language='text').classes('m-0')
                except Exception:
                    ui.label(subject)
        else:
            # Default to SQL query presentation
            pretty = App.format_sql(rec.get('query', ''))
            with ui.expansion('SQL', icon='data_object', value=True).classes('w-full'):
                ui.code(pretty, language='sql').classes('m-0')

    def _render_oracle_stacktrace_section(self, rec: dict) -> None:
        stack_text = '\n'.join(rec.get('stacktrace') or [])
        if not stack_text:
            return
        with ui.expansion('Stacktrace', icon='troubleshoot', value=True).classes('w-full'):
            ui.codemirror(value=stack_text, line_wrapping=True).classes('w-full stacktrace-cm')

    def _render_oracle_report_section(self, rec: dict) -> None:
        report_text = (rec.get('report') or rec.get('last_message'))
        if not report_text:
            return
        cleaned_report = re.sub(r'^\s*(?:RELEVANT|IRRELEVANT|UNSURE)\s*', '', str(report_text), count=1, flags=re.IGNORECASE)
        with ui.expansion('Report', icon='description', value=True).classes('w-full'):
            try:
                ui.markdown(cleaned_report)
            except Exception:
                ui.label(cleaned_report)

    def _render_oracle_stdio_section(self, rec: dict) -> None:
        if (stdout := rec.get('stdout')):
            with ui.expansion('stdout', icon='terminal').classes('w-full'):
                self.render_terminal(stdout)
        if (stderr := rec.get('stderr')):
            with ui.expansion('stderr', icon='terminal').classes('w-full'):
                self.render_terminal(stderr)

    def _render_oracle_outputs(self, *, parent=None) -> None:
        """Render the oracle outputs tab contents inside the given parent or current context."""
        container = parent or self.timeline_container
        records, chosen_dir = self._load_oracle_records()
        with container:
            if not records:
                msg = 'No oracle outputs found.'
                if chosen_dir is None:
                    msg += " Expected directory 'oracle-logs'."
                ui.label(msg).classes('text-caption text-grey')
                return

            self._oracle_summary_chart(records)
            ui.separator()
            grid, by_digest = self._oracle_grid(records)
            rec_dlg, rec_dlg_container = self._oracle_record_dialog()
            grid.on('rowDoubleClicked', lambda e: self._on_oracle_row_double_clicked(e, by_digest, rec_dlg, rec_dlg_container), args=['data'])

    def _oracle_summary_chart(self, records: List[dict]) -> None:
        counts = Counter(r['verdict'] for r in records)
        order = [Verdict.RELEVANT, Verdict.IRRELEVANT, Verdict.UNSURE, Verdict.UNKNOWN]
        labels = [v.value for v in order]
        values = [int(counts.get(v, 0)) for v in order]
        colors = ['#21ba45', '#c10015', '#f2c037', '#9e9e9e']
        stacked_series = [
            {
                'name': labels[i],
                'type': 'bar',
                'stack': 'total',
                'data': [values[i]],
                'itemStyle': {'color': colors[i]},
                'label': {
                    'show': bool(values[i] > 0),
                    'position': 'inside',
                    'formatter': '{c}',
                    'color': '#fff'
                },
                'emphasis': {'focus': 'series'},
                'barWidth': '70%'
            }
            for i in range(len(order))
        ]
        ui.echart({
            'animation': False,
            'tooltip': {'trigger': 'item', 'appendToBody': True, 'confine': False},
            'color': colors,
            'legend': {
                'data': labels,
                'bottom': 0,
                'left': 'center',
                'orient': 'horizontal',
                'itemHeight': 8,
                'itemWidth': 16,
                'textStyle': {'color': '#666', 'fontSize': 12},
                'selectedMode': False,
            },
            'grid': {'left': 6, 'right': 24, 'top': 6, 'bottom': 24, 'containLabel': True},
            'xAxis': {'type': 'value', 'axisLabel': {'show': False}, 'axisTick': {'show': False}, 'axisLine': {'show': False}, 'splitLine': {'show': False}},
            'yAxis': {'type': 'category', 'data': [''], 'axisLabel': {'show': False}, 'axisTick': {'show': False}, 'axisLine': {'show': False}},
            'series': stacked_series,
        }).classes('w-full max-w-[560px] mx-auto').style('height: 64px')

    def _oracle_grid(self, records: List[dict]):
        grid = ui.aggrid({
            'defaultColDef': {'resizable': True, 'sortable': True},
            'animateRows': True,
            'ensureDomOrder': True,
            'suppressCellFocus': True,
            'suppressMovableColumns': True,
            'tooltipShowDelay': 300,
            'columnDefs': [
                {'headerName': '#', 'field': 'row_idx', 'width': 70, 'minWidth': 60, 'maxWidth': 90, 'pinned': 'left', 'sortable': False, 'resizable': False, 'suppressMenu': True, 'type': 'rightAligned', 'cellClass': 'text-right text-grey'},
                {
                    'headerName': 'Subject',
                    'field': 'subject_preview',
                    'tooltipField': 'subject',
                    'flex': 2,
                    'minWidth': 320,
                    # Render a badge for kind + truncated subject text
                    'cellRenderer': 'function(params){\n'
                                     '  const k = (params.data && params.data.oracle) || "";\n'
                                     '  const kind = k === "codex-conditional" ? "Conditional" : "Query";\n'
                                     '  const color = kind === "Conditional" ? "bg-amber-600" : "bg-indigo-600";\n'
                                     '  const esc = s => String(s || "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));\n'
                                     '  const text = esc(params.value || "");\n'
                                     '  return `<span class=\"inline-flex items-center gap-2 w-full\">` +\n'
                                     '         `<span class=\"inline-block px-2 py-[2px] rounded text-white text-xs ${color}\">${kind}</span>` +\n'
                                     '         `<span class=\"font-mono truncate flex-1 min-w-0\">${text}</span>` +\n'
                                     '         `</span>`;\n'
                                     '}'
                },
                {'headerName': 'Verdict', 'field': 'verdict', 'width': 110, 'minWidth': 100, 'cellClass': 'text-center font-medium', 'cellStyle': 'function(){ return {textAlign: "center"}; }', 'cellClassRules': {'text-positive': "data.verdict === 'Relevant'", 'text-negative': "data.verdict === 'Irrelevant'", 'text-warning': "data.verdict === 'Unsure'", 'text-gray-700': "data.verdict === 'Unknown'"}},
                {'headerName': 'Duration (s)', 'field': 'dur_s', 'width': 110, 'minWidth': 100, 'type': 'numericColumn'},
                {'headerName': 'Tokens', 'field': 'tokens_used', 'width': 140, 'minWidth': 120, 'type': 'numericColumn'},
                {'headerName': 'Exit', 'field': 'exit_code', 'width': 80, 'minWidth': 70, 'type': 'numericColumn'},
            ],
            'rowData': records,
            'rowSelection': 'single',
            'domLayout': 'autoHeight',
        }).classes('w-full max-w-none flex-1')
        by_digest: Dict[str, dict] = {r['key_digest']: r for r in records}
        return grid, by_digest

    def _oracle_record_dialog(self):
        with ui.dialog() as rec_dlg:
            rec_dlg.props('maximized no-refocus')
            with ui.card().classes('w-screen h-screen max-w-none max-h-none p-2 flex flex-col gap-2 overflow-hidden'):
                with ui.row().classes('items-center justify-between w-full'):
                    ui.label('Oracle Record Details').classes('text-subtitle1')
                    ui.button(icon='close', on_click=rec_dlg.close).props('flat round dense')
                rec_dlg_container = ui.element('div').classes('flex-1 min-h-0 w-full overflow-auto')
        return rec_dlg, rec_dlg_container

    def _on_oracle_row_double_clicked(self, e, by_digest: Dict[str, dict], rec_dlg, rec_dlg_container) -> None:
        try:
            data = None
            if hasattr(e, 'args'):
                if isinstance(e.args, dict):
                    data = e.args.get('data') or e.args.get('row')
                elif isinstance(e.args, list) and e.args:
                    data = e.args[0]
            if data is None and hasattr(e, 'args') and isinstance(e.args, dict):
                node = e.args.get('node')
                if isinstance(node, dict):
                    data = node.get('data')
            kd = data.get('key_digest') if isinstance(data, dict) else None
            if not kd:
                return
            rec = by_digest.get(kd)
            if not rec:
                return
            rec_dlg_container.clear()
            with rec_dlg_container:
                self._render_oracle_record_details_content(rec)
            rec_dlg.open()
        except Exception:
            pass

def validate_run_dir(s: str) -> Path:
    p = Path(s).expanduser()
    if not p.is_dir():
        raise argparse.ArgumentTypeError(f"run directory not found: {p}")
    if not (paths_dir := (p / 'annotated-paths')).is_dir():
        raise argparse.ArgumentTypeError(f"run directory does not contain 'annotated-paths' subdirectory: {p}")
    try:
        files = _list_input_files(paths_dir)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    if not files:
        pats = ', '.join(INPUT_FILE_GLOB_PATTERNS)
        raise argparse.ArgumentTypeError(f"no input files matching any of [{pats}] found in '{paths_dir}'")
    return p.expanduser()
