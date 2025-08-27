import asyncio
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, TypedDict, Literal

import duckdb
import numpy as np
import pandas as pd
import sqlparse
from nicegui import ui
import html


# ------------------ Data & Indexing ------------------


def _list_input_files(data_dir: str) -> List[str]:
    base = Path(data_dir).expanduser()
    return sorted([str(p) for p in base.glob("paths-with-conds-*.json.gz")])


def _sql_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def build_full_index(
    data_dir: str,
    index_path: str,
    threads: int = 8,
    memory_limit: str = "system",
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> None:
    base = Path(data_dir).expanduser()
    files = sorted(str(p) for p in base.glob("paths-with-conds-*.json.gz"))
    if not files:
        raise RuntimeError("No input files found to index.")

    con = duckdb.connect(index_path)
    con.execute(f"PRAGMA threads={int(threads)}")
    con.execute("PRAGMA preserve_insertion_order=false")
    con.execute(f"PRAGMA memory_limit={_sql_quote(memory_limit)}")

    # Ensure a temp directory exists for spill-to-disk operations.
    tmpdir = Path("display_annotated_paths_nicegui/.duckdb_tmp")
    try:
        tmpdir.mkdir(parents=True, exist_ok=True)
        tmp_sql = str(tmpdir).replace("'", "''")
        con.execute(f"PRAGMA temp_directory='{tmp_sql}'")
    except Exception:
        pass

    def events_select_sql(file_literal: str) -> str:
        return f"""
        WITH exploded AS (
          SELECT r.filename AS file, r.runId,
                 i AS event_idx,
                 list_extract(r.aes, i) AS ev
          FROM read_json_auto({file_literal}, filename=true) r,
               range(array_length(r.aes)) idx(i)
        )
        SELECT
          file,
          runId,
          event_idx,
          struct_extract(ev, 'vacuousness') AS vacuousness,
          struct_extract(struct_extract(ev, 'elem'), '$type') AS type,
          struct_extract(struct_extract(ev, 'elem'), 'qIdx') AS qIdx,
          struct_extract(struct_extract(ev, 'elem'), 'query') AS query,
          struct_extract(struct_extract(ev, 'elem'), 'params') AS params,
          struct_extract(struct_extract(ev, 'elem'), 'stacktrace') AS stacktrace,
          struct_extract(struct_extract(ev, 'elem'), 'cond') AS cond,
          struct_extract(struct_extract(ev, 'elem'), 'outcome') AS outcome
        FROM exploded
        WHERE ev IS NOT NULL
        """

    total = len(files)
    if progress_cb:
        progress_cb(0, total)

    first = files[0]
    first_lit = _sql_quote(first)
    con.execute(f"CREATE OR REPLACE TABLE events AS {events_select_sql(first_lit)};")
    if progress_cb:
        progress_cb(1, total)

    for i, f in enumerate(files[1:], start=1):
        flit = _sql_quote(f)
        con.execute(f"INSERT INTO events {events_select_sql(flit)};")
        if progress_cb:
            progress_cb(i + 1, total)

    con.execute(
        """
        CREATE OR REPLACE TABLE traces AS
        SELECT runId,
               any_value(file) AS file,
               count(*) AS n_events,
               count(*) FILTER (WHERE type = 'SqlQueryDecl') AS n_sql,
               count(*) FILTER (WHERE type = 'PathConditionAtom') AS n_conds
        FROM events
        GROUP BY runId;
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE queries AS
        SELECT runId, qIdx, lower(query) AS query_lc
        FROM events WHERE type = 'SqlQueryDecl';
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TABLE conds AS
        SELECT runId, struct_extract(cond, 'op') AS op, CAST(outcome AS BOOLEAN) AS outcome
        FROM events WHERE type = 'PathConditionAtom';
        """
    )

    con.close()


@dataclass
class DuckDBConfig:
    threads: int = 4
    memory_limit: str = '8GB'
    preserve_insertion_order: bool = False


@dataclass
class AppState:
    data_dir: str = ''
    index_path: str = ''
    sql_sub: str = ''
    min_sql: int = 0
    min_conds: int = 0
    show_details: bool = False
    duckdb: DuckDBConfig = field(default_factory=DuckDBConfig)
    run_id: Optional[int] = None


# ------------------ Formatting helpers ------------------
def _preview(text: str | None, length: int = 80) -> str:
    """Return a short, single-line preview with ellipsis if the text is long."""
    if text is None:
        return ""
    return text if len(text) <= length else text[: length - 1] + "…"


def _vac_badge(v: str) -> ui.badge:
    match v:
        case 'Vacuous':
            color = 'red'
        case 'NonVacuous':
            color = 'green'
        case _:
            raise ValueError(f"Unknown vacuousness: {v}")

    return ui.badge(v, color=color).props('outline')


def _tf_badge(outcome: bool) -> ui.badge:
    text = 'T' if bool(outcome) else 'F'
    color = 'primary'
    return ui.badge(text, color=color).props('outline')


def _q_badge(qi: int) -> ui.badge:
    """Standard Q-badge: white text on purple background, dense."""
    return ui.badge(f'Q{int(qi)}', color='purple').props('text-color=white dense')


def _fmt_qrv(term: dict) -> str:
    q = term["qIdx"]["value"]
    r = term["rowIdx"]["value"]
    c = term["colIdx"]["value"]
    rep = f"Q{q}R{r}C{c}"
    if (name := term.get("colName")) is not None:
        rep = f"{rep}[{name}]"
    return rep


def _fmt_term(term) -> str:
    return f"`{_fmt_term_inner(term)}`"


def _fmt_term_inner(term) -> str:
    if term == "ConstTrue":
        return "true"
    if term == "ConstFalse":
        return "false"

    if not isinstance(term, dict):
        raise ValueError(f"Unexpected term type: {term}")

    match term["$type"]:
        case "ConstString":
            v = term["value"]
            return f'"{v}"'
        case "ConstLong":
            return str(term.get("value"))
        case "QueryResVar":
            return _fmt_qrv(term)
        case "DeclaredVar":
            return term["name"]
        case "UnaryOp":
            op = term["op"]
            operand = _fmt_term_inner(term.get("operand"))
            return f"{op}({operand})"
        case "BinaryOp":
            lhs = _fmt_term_inner(term.get("lhs"))
            rhs = _fmt_term_inner(term.get("rhs"))
            op = term["op"]
            if op == "Eq":
                return f"{lhs} = {rhs}"
            else:
                return f"{op}({lhs}, {rhs})"
        case ty:
            raise ValueError(f"Unknown term type: {ty}")


def _json_default(x):  # best-effort fallback for pretty JSON
    try:
        if isinstance(x, (np.integer, np.floating)):
            return x.item()
    except Exception:
        pass
    if isinstance(x, (set, tuple)):
        return list(x)
    try:
        return str(x)
    except Exception:
        return repr(x)



def _params_to_rows(params) -> List[Dict[str, str]]:
    if params is None:
        return []

    items = list(params)
    if not items:
        return []

    out: List[Dict[str, str]] = []
    for item in items:
        term = json.loads(item)
        out.append({"Param": _fmt_term(term)})
    return out

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
        return [{'kind': 'text', 'text': str(term)}]

    ty = term.get("$type")
    if ty == "ConstString":
        return [{'kind': 'text', 'text': f"\"{term['value']}\""}]
    if ty == "ConstLong":
        return [{'kind': 'text', 'text': str(term.get('value'))}]
    if ty == "DeclaredVar":
        return [{'kind': 'text', 'text': term["name"]}]
    if ty == "QueryResVar":
        q = term["qIdx"]["value"]; r = term["rowIdx"]["value"]; c = term["colIdx"]["value"]
        rep = f"Q{q}R{r}C{c}"
        if (name := term.get("colName")) is not None:
            rep = f"{rep}[{name}]"
        return [{'kind': 'qrv', 'text': rep, 'q': q}]
    if ty == "UnaryOp":
        op = term["op"]
        return [{'kind': 'text', 'text': f'{op}('}] + term_to_frags(term.get('operand')) + [{'kind': 'text', 'text': ')'}]
    if ty == "BinaryOp":
        fr_l = term_to_frags(term.get('lhs'))
        fr_r = term_to_frags(term.get('rhs'))
        if term.get('op') == 'Eq':
            return fr_l + [{'kind': 'text', 'text': ' = '}] + fr_r
        else:
            return ([{'kind': 'text', 'text': f"{term.get('op')}("}] +
                    fr_l + [{'kind': 'text', 'text': ', '}] +
                    fr_r + [{'kind': 'text', 'text': ')'}])
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
    def __init__(self, default_data_dir: Optional[str] = None) -> None:
        self.state = AppState()
        if default_data_dir:
            self.state.data_dir = default_data_dir
            self.state.index_path = str(Path(default_data_dir).expanduser() / 'ap_index.duckdb')

        # UI elements we'll update
        self.traces_table = None
        self.run_id_input = None
        self.timeline_container = None
        self.files_found_label = None
        self.index_status_container = None

        # Progress state for index build
        self._build_progress = {"done": 0, "total": 1}
        self._build_timer = None

        self._build_ui()
        self._refresh_files_found()
        self._refresh_index_status()
        self.refresh_traces()

    # ------------- Path display helpers -------------

    @staticmethod
    def shorten_path_for_display(file_path: object, data_dir: Optional[str]) -> str:
        """Return a short display path for a file.

        If a data directory is provided, prefer a path relative to it; otherwise
        fall back to the file name. Accepts any object that can be cast to str.
        """
        try:
            file_str = str(file_path)
        except Exception:
            try:
                return repr(file_path)
            except Exception:
                return ""

        if not file_str:
            return ""

        try:
            if data_dir:
                base = Path(data_dir).expanduser()
                try:
                    return str(Path(file_str).resolve().relative_to(base.resolve()))
                except Exception:
                    try:
                        return str(Path(file_str).relative_to(base))
                    except Exception:
                        import os
                        return os.path.relpath(file_str, str(base))
            # no data_dir: use just the file name
            return Path(file_str).name
        except Exception:
            return file_str

    # ------------- Data helpers -------------

    def _connect_index(self) -> Optional[duckdb.DuckDBPyConnection]:
        if duckdb is None:
            return None
        idx = self.state.index_path.strip()
        if not idx:
            return None
        p = Path(idx).expanduser()
        if not p.exists():
            return None
        con = duckdb.connect(str(p))
        cfg = self.state.duckdb
        con.execute(f"PRAGMA threads={int(cfg.threads)}")
        con.execute(f"PRAGMA preserve_insertion_order={'true' if cfg.preserve_insertion_order else 'false'}")
        ml = str(cfg.memory_limit).replace("'", "''")
        con.execute(f"PRAGMA memory_limit='{ml}'")
        return con

    # ------------- UI construction -------------

    def _build_ui(self) -> None:
        # Top-level drawer (must not be nested under header)
        self._drawer = ui.left_drawer(value=True, fixed=True).classes('bg-grey-2')

        with self._drawer:
            with ui.column().classes('px-3 py-2 gap-2'):
                ui.label('Filters').classes('text-subtitle2')

                data_input = ui.input('Data directory', value=self.state.data_dir, placeholder='Path to annotated-paths directory')
                self.files_found_label = ui.label('')

                def on_data_change(e):
                    self.state.data_dir = (data_input.value or '').strip()
                    # Suggest default index alongside data
                    if self.state.data_dir:
                        self.state.index_path = str(Path(self.state.data_dir) / 'ap_index.duckdb')
                        index_input.value = self.state.index_path
                        index_input.update()
                    self._refresh_files_found()
                data_input.on('change', on_data_change)

                ui.separator()
                ui.label('Index').classes('text-subtitle2')
                index_input = ui.input('Index path (.duckdb)', value=self.state.index_path)

                async def on_build_click():
                    if duckdb is None:
                        ui.notify('duckdb not installed: pip install duckdb', color='negative')
                        return
                    self.state.index_path = (index_input.value or '').strip()
                    self.state.data_dir = (data_input.value or '').strip()
                    files = _list_input_files(self.state.data_dir) if self.state.data_dir else []
                    if not files:
                        ui.notify('No files found to index', color='warning')
                        return

                    # Progress dialog
                    with ui.dialog() as dlg, ui.card().classes('min-w-[420px]'):
                        ui.label('Building DuckDB index')
                        prog = ui.linear_progress(value=0)
                        prog_text = ui.label('0/0 files')

                    self._build_progress = {"done": 0, "total": max(1, len(files))}

                    def _update_progress():
                        done = self._build_progress['done']
                        total = max(1, self._build_progress['total'])
                        prog.value = min(1.0, done / total)
                        prog_text.text = f"{done}/{total} files"
                        prog.update(); prog_text.update()

                    def _cb(done: int, total: int):
                        self._build_progress['done'] = done
                        self._build_progress['total'] = max(1, total)

                    async def _run_build():
                        dlg.open()
                        # periodic UI updates while building in thread
                        self._build_timer = ui.timer(0.2, _update_progress)
                        try:
                            await asyncio.to_thread(
                                build_full_index,
                                data_dir=self.state.data_dir,
                                index_path=self.state.index_path,
                                threads=int(self.state.duckdb.threads),
                                memory_limit=str(self.state.duckdb.memory_limit) or 'system',
                                progress_cb=_cb,
                            )
                            self._build_progress['done'] = self._build_progress['total']
                            _update_progress()
                            ui.notify('Index build complete', color='positive')
                        except Exception as e:  # pragma: no cover
                            ui.notify(f'Index build failed: {e}', color='negative', close_button=True)
                        finally:
                            if self._build_timer:
                                self._build_timer.cancel()
                            dlg.close()
                            self._refresh_index_status()
                            self.refresh_traces()

                    await _run_build()

                ui.button('Build/Refresh Full Index', on_click=on_build_click).props('color=primary')

                def on_index_change(e):
                    self.state.index_path = (index_input.value or '').strip()
                    self._refresh_index_status()
                    self.refresh_traces()
                index_input.on('change', on_index_change)

                ui.separator()
                sql_input = ui.input('SQL contains', value=self.state.sql_sub)
                sql_input.on('change', lambda e: self._on_filter_change(sql=sql_input.value or ''))

                min_sql_input = ui.number('Min #SQL', value=self.state.min_sql, format='%.0f').props('dense')
                min_sql_input.on('change', lambda e: self._on_filter_change(min_sql=int(min_sql_input.value or 0)))

                min_conds_input = ui.number('Min #Conds', value=self.state.min_conds, format='%.0f').props('dense')
                min_conds_input.on('change', lambda e: self._on_filter_change(min_conds=int(min_conds_input.value or 0)))

                ui.checkbox('Show query details', value=self.state.show_details,
                            on_change=lambda e: self._on_filter_change(show_details=bool(e.value)))

                with ui.expansion('Advanced (DuckDB)', icon='tune'):
                    thr = ui.number('threads', value=self.state.duckdb.threads, min=1, max=64, format='%.0f')
                    thr.on('change', lambda e: self._on_duckdb_change(threads=int(thr.value or 1)))
                    ml = ui.input('memory_limit', value=self.state.duckdb.memory_limit)
                    ml.on('change', lambda e: self._on_duckdb_change(memory_limit=str(ml.value or '8GB')))
                    pio = ui.checkbox('preserve_insertion_order', value=self.state.duckdb.preserve_insertion_order)
                    pio.on('change', lambda e: self._on_duckdb_change(preserve_insertion_order=bool(pio.value)))

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
        if 'show_details' in kwargs:
            self.state.show_details = kwargs['show_details']
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
        n = len(_list_input_files(self.state.data_dir)) if self.state.data_dir else 0
        self.files_found_label.text = f"Found {n} files"
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

            ip = Path(self.state.index_path).expanduser() if self.state.index_path else None
            if ip and ip.exists():
                try:
                    size_mb = ip.stat().st_size / (1024 * 1024)
                    ui.label(f"Index: {ip} ({size_mb:.1f} MB)").classes('text-caption')
                except Exception:
                    ui.label(f"Index: {ip}").classes('text-caption')

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
            where.append("EXISTS (SELECT 1 FROM events e WHERE e.runId=t.runId AND e.type='SqlQueryDecl' AND lower(e.query) LIKE ?)")
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
                df['file'] = df['file'].map(lambda p: App.shorten_path_for_display(p, self.state.data_dir))
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
                "SELECT event_idx, type, qIdx, query, params, stacktrace, vacuousness, cond, outcome "
                "FROM events WHERE runId = ? ORDER BY event_idx",
                [run_id],
            ).fetchdf()
        finally:
            con.close()

        # Header metrics
        if summary is not None and not summary.empty:
            s = summary.iloc[0]
            with self.timeline_container:
                with ui.card().classes('w-full'):
                    ui.label(f"Run {int(run_id)}").classes('text-subtitle1')
                    # Show file path relative to the selected data directory for brevity
                    file_disp = self.shorten_path_for_display(s.get('file', ''), self.state.data_dir)
                    ui.label(f"file: {file_disp}").classes('text-caption')
                    with ui.row().classes('gap-6'):
                        for label, val in [('events', int(s['n_events'])), ('sql queries', int(s['n_sql'])), ('conditions', int(s['n_conds']))]:
                            with ui.card().classes('py-1 px-3'):
                                ui.label(label).classes('text-caption text-grey')
                                ui.label(str(val)).classes('text-body1')
        else:
            with self.timeline_container:
                ui.label(f"Run {int(run_id)} not found in index").classes('text-negative')

        with self.timeline_container:
            self._render_events(ev_df)

    def _render_events(self, ev_df) -> None:
        if ev_df is None or ev_df.empty:
            ui.label('(no events)')
        else:
            # Precompute qi -> SQL for tooltips (pretty and raw)
            queries_by_qi: Dict[int, str] = {}
            for _, _row in ev_df.iterrows():
                if _row['type'] == 'SqlQueryDecl':
                    _qi = _row['qIdx']['value']
                    queries_by_qi[_qi] = str(_row.get('query') or '')
            pretty_by_qi: Dict[int, str] = {qi: sqlparse.format(q, reindent=True, keyword_case='upper') for qi, q in queries_by_qi.items()}

            row_counters: Counter[int] = Counter()
            current_query_card = None

            for _, r in ev_df.iterrows():
                match r['type']:
                    case 'SqlQueryDecl':
                        # Start a new card for a query declaration
                        qi = r['qIdx']['value']
                        if current_query_card is not None:
                            raise ValueError(f"Unexpected nested SqlQueryDecl for Q{qi}")
                        with (current_query_card := ui.card().props(f'id=q-{qi}').classes('w-full')):
                            # 3-column grid: [index] [Q badge] [content]. Parameters begin under the badge (col 2).
                            one_line = ' '.join(r['query'].splitlines())
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
                                    pretty = sqlparse.format(r['query'], reindent=True, keyword_case='upper')
                                    ui.code(pretty, language='sql').classes('mt-0 min-w-0')

                                # Row 2: Parameters start under the badge (col 2), spanning cols 2-3
                                params = r.get('params')
                                if params is not None and getattr(params, 'size', 0) > 0:
                                    with ui.row().classes('items-center gap-2 flex-wrap col-start-2 col-span-2'):
                                        ui.label('Parameters:').classes('text-caption')
                                        for i, item in enumerate(params, 1):
                                            term = json.loads(item)
                                            frags = term_to_frags(term)
                                            with ui.row().classes('items-center gap-1 px-2 py-[2px] rounded border border-grey-5'):
                                                render_frags(frags, pretty_by_qi=pretty_by_qi)

                            # Details
                            if self.state.show_details:
                                if stacktrace := r.get('stacktrace'):
                                    with ui.expansion('Stacktrace', icon='stacked_bar_chart'):
                                        ui.code(stacktrace, language='text')
                    case 'SqlQueryResRowDecl':
                        qi = r['qIdx']['value']
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
                        qi = r['qIdx']['value']
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
                                _tf_badge(bool(r.get('outcome')))
                                _vac_badge(r.get('vacuousness'))
                                frags = term_to_frags(r["cond"])
                                with ui.row().classes('items-center gap-1 flex-wrap'):
                                    render_frags(frags, pretty_by_qi=pretty_by_qi)
                    case _:
                        raise ValueError(f"Unknown event type: {r['type']}")


def main() -> None:
    default_data_dir = sys.argv[1] if len(sys.argv) > 1 else None
    App(default_data_dir=default_data_dir)
    ui.run(title='Annotated Paths Browser')


if __name__ in {"__main__", "__mp_main__"}:
    main()
