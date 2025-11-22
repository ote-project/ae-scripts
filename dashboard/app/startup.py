#!/usr/bin/env python3
import argparse
import asyncio
import html
import json
import os
import queue
import re
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
from .metrics import render_metrics_tab
from .index_builder import build_full_index, _list_input_files, INPUT_FILE_GLOB_PATTERNS


class Verdict(Enum):
    RELEVANT = 'Relevant'
    IRRELEVANT = 'Irrelevant'
    UNSURE = 'Unsure'
    UNKNOWN = 'Unknown'


class BranchRelevance(Enum):
    RELEVANT = 'Relevant'
    IRRELEVANT = 'Irrelevant'
    UNKNOWN = 'Unknown'

    @classmethod
    def from_value(cls, value: object) -> 'BranchRelevance':
        if isinstance(value, BranchRelevance):
            return value
        if value is None:
            return cls.UNKNOWN
        text = str(value).strip().lower()
        if text == 'relevant':
            return cls.RELEVANT
        if text == 'irrelevant':
            return cls.IRRELEVANT
        return cls.UNKNOWN


# ------------------ Data & Indexing ------------------
# Import index-building utilities; keep MAX_ONE_LINE_SQL_LEN local for UI formatting
MAX_ONE_LINE_SQL_LEN = 120


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
    # Event timeline filters
    show_cond_atoms: bool = True
    show_query_events: bool = True
    hide_vacuous: bool = False


# ------------------ Formatting helpers ------------------
def _vac_badge(v: str) -> ui.badge:
    match v:
        case 'Vacuous':
            text = 'Vacuous'
            return ui.badge(text, color='grey').props('outline dense')
        case 'NonVacuous':
            text = 'NonVacu'
            return ui.badge(text, color='primary').props('text-color=white dense')
        case _:
            raise ValueError(f"Unknown vacuousness: {v}")


def _q_badge(qi: int) -> ui.badge:
    """Standard Q-badge: black text on white background with black border."""
    return ui.badge(f'Q{int(qi)}').props('dense').classes('bg-white text-black border border-black')

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
        self.review_state_path = self.run_dir / 'oracle-reviewed.json'
        self.review_state = self._load_review_state()

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
        self.tab_metrics = None
        self.sidebar_runs = None
        self.metrics_container = None

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
        css = """
            .cm-editor{width:100%}
            .cm-scroller{overflow:auto}
            .cm-content{min-width:0!important}
            .prompt-cm .cm-editor{min-height:18rem}
            .stacktrace-cm .cm-editor{min-height:18rem}
            .stacktrace-dialog .cm-editor{height:100%}

            /* Keep Quasar pages full-width */
            .q-page, .q-page-container{max-width:none!important}

            /* Layout: let QLayout own the viewport; make containers flex & shrinkable */
            html, body, .q-app { height: 100% }
            .q-layout { min-height: 100vh; display:flex; flex-direction:column }
            .q-page-container{flex:1 1 auto;display:flex;flex-direction:column;min-height:0}
            .q-page{flex:1 1 auto;display:flex;flex-direction:column;min-height:0}

            /* Tabs must stretch to fill remaining height */
            .q-tab-panels{flex:1 1 auto;min-height:0}
            .q-tab-panels .q-tab-panel{display:flex;flex-direction:column;min-height:0;height:100%}

            /* AG Grid should fill its container and scroll internally */
            .oracle-grid{width:100%!important;max-width:none!important;height:100%!important;min-height:0;overflow:hidden}
            .oracle-grid .ag-root-wrapper,
            .oracle-grid .ag-root-wrapper-body{width:100%!important;height:100%!important;overflow:hidden}
            .oracle-grid .ag-center-cols-viewport{overflow:auto!important}
            .oracle-grid .ag-body-viewport{overflow:auto!important}
            .oracle-grid .oracle-row-reviewed .ag-cell,
            .oracle-grid .oracle-row-reviewed .ag-group-contracted,
            .oracle-grid .oracle-row-reviewed .ag-group-expanded{
                background-color:#f0faf4;
            }
        """
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

        # Event-type filters for the timeline
        self.show_query_checkbox = ui.switch('Show queries', value=self.state.show_query_events)
        self.show_query_checkbox.on_value_change(lambda e: self._on_filter_change(show_query_events=bool(getattr(e, 'value', self.show_query_checkbox.value))))
        self.show_cond_checkbox = ui.switch('Show conditionals', value=self.state.show_cond_atoms)
        self.show_cond_checkbox.on_value_change(lambda e: self._on_filter_change(show_cond_atoms=bool(getattr(e, 'value', self.show_cond_checkbox.value))))
        self.hide_vacuous_checkbox = ui.switch('Hide vacuous', value=self.state.hide_vacuous)
        self.hide_vacuous_checkbox.on_value_change(lambda e: self._on_filter_change(hide_vacuous=bool(getattr(e, 'value', self.hide_vacuous_checkbox.value))))

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
                self.tab_metrics = ui.tab('Metrics')

    def _build_main_panels(self) -> None:
        # Main content with top-level tabs for Runs and Oracle
        with ui.row().classes('px-4 py-2 gap-4 w-full max-w-none items-stretch flex-1 min-h-0').style('width: 100%'):
            with ui.column().classes('w-full gap-3 max-w-none flex-1 min-h-0'):
                # Panels are driven by the tabs in the header
                with ui.tab_panels(self.main_tabs, value=self.tab_runs).classes('w-full flex-1 min-h-0'):
                    with ui.tab_panel(self.tab_runs):
                        # Timeline container for run details
                        self.timeline_container = ui.column().classes('gap-2 w-full flex-1 min-h-0')
                    with ui.tab_panel(self.tab_oracle):
                        # Global Oracle view (independent of runs/index)
                        self.oracle_container = ui.column().classes('w-full flex-1 min-h-0')
                        with self.oracle_container:
                            self._render_oracle_outputs(parent=self.oracle_container)
                    with ui.tab_panel(self.tab_metrics):
                        # Metrics view
                        self.metrics_container = ui.column().classes('w-full flex-1 min-h-0')
                        with self.metrics_container:
                            render_metrics_tab(self.run_dir)

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
    def _row_value(row, key: str):
        """Best-effort fetch of a key from a pandas Series or dict, None-safe."""
        if isinstance(row, dict):
            candidate = row.get(key)
        else:
            getter = getattr(row, 'get', None)
            candidate = None
            if callable(getter):
                with suppress(Exception):
                    candidate = getter(key)
        if candidate is None:
            with suppress(Exception):
                candidate = row[key]
        if candidate is None:
            return None
        try:
            # Treat pandas NaN/NA as missing
            if pd.isna(candidate):
                return None
        except Exception:
            pass
        return candidate

    @staticmethod
    def _branch_relevance_from_row(row) -> BranchRelevance:
        return BranchRelevance.from_value(App._row_value(row, 'relevance'))

    @staticmethod
    def _branch_relevance_badge(relevance: BranchRelevance):
        if relevance == BranchRelevance.UNKNOWN:
            return None
        if relevance == BranchRelevance.RELEVANT:
            return ui.badge(relevance.value, color='positive').props('text-color=white dense')
        return ui.badge(relevance.value).props('outline color=negative text-color=negative dense')

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
        """Handle filter changes from the sidebar.

        - SQL/min_* filters affect the Traces table and should reload it.
        - Timeline filters (show_* checkboxes) should only re-render the current run's events.
        """
        traces_filters_changed = False
        timeline_filters_changed = False

        if 'sql' in kwargs:
            self.state.sql_sub = kwargs['sql']
            traces_filters_changed = True
        if 'min_sql' in kwargs:
            self.state.min_sql = kwargs['min_sql']
            traces_filters_changed = True
        if 'min_conds' in kwargs:
            self.state.min_conds = kwargs['min_conds']
            traces_filters_changed = True
        if 'show_cond_atoms' in kwargs:
            self.state.show_cond_atoms = bool(kwargs['show_cond_atoms'])
            timeline_filters_changed = True
        if 'show_query_events' in kwargs:
            self.state.show_query_events = bool(kwargs['show_query_events'])
            timeline_filters_changed = True
        if 'hide_vacuous' in kwargs:
            self.state.hide_vacuous = bool(kwargs['hide_vacuous'])
            timeline_filters_changed = True

        if traces_filters_changed:
            # Reload traces list and re-render run detail
            self.refresh_traces()
        elif timeline_filters_changed:
            # Only re-render the current run's events timeline
            self._render_run_detail()

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
                current_query_relevance=BranchRelevance.UNKNOWN,
                pretty_by_qi=pretty_by_qi,
                oracle_by_digest=oracle_by_digest,
            )

            handlers = {
                'SqlQueryDecl': self._ev_sql_decl,
                'SqlQueryResRowDecl': self._ev_sql_row,
                'SqlQueryResEnd': self._ev_sql_end,
                'PathConditionAtom': self._ev_path_cond,
            }

            # Apply optional type filtering based on sidebar checkboxes
            allowed_types = None
            if not (self.state.show_cond_atoms and self.state.show_query_events):
                allowed_types = set()
                if self.state.show_query_events:
                    allowed_types.update({'SqlQueryDecl', 'SqlQueryResRowDecl', 'SqlQueryResEnd'})
                if self.state.show_cond_atoms:
                    allowed_types.add('PathConditionAtom')

            shown = 0
            for _, r in ev_df.iterrows():
                elem = r['elem']
                et = elem['$type']
                if allowed_types is not None and et not in allowed_types:
                    continue
                # Apply vacuousness filtering if enabled
                if self.state.hide_vacuous and r.get('vacuousness') == 'Vacuous':
                    continue
                fn = handlers.get(et)
                if fn is None:
                    raise ValueError(f"Unknown event type: {r['type']}")
                fn(r, elem, ctx)
                shown += 1

            if shown == 0:
                ui.label('(no events match filters)').classes('text-grey')

    @dataclass
    class QueryContext:
        row_counters: Counter
        current_query_card: Optional[object]
        current_query_irrelevant_badged: bool
        current_query_relevance: BranchRelevance
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
        row_relevance = self._branch_relevance_from_row(r)
        ctx.current_query_relevance = row_relevance
        ctx.current_query_irrelevant_badged = row_relevance == BranchRelevance.IRRELEVANT
        card = ui.card().props(f'id=q-{qi}').classes('w-full')
        ctx.current_query_card = card
        with card:
            one_line, is_short = App.sql_one_liner(elem['query'])
            grid_row_align = 'items-center' if is_short else 'items-start'
            with ui.grid().classes(f'grid-cols-[auto,auto,1fr] gap-x-2 gap-y-1 {grid_row_align} w-full'):
                ui.label(f"[{r['event_idx']}]").classes('text-caption text-grey' + (' self-center' if is_short else ''))
                with ui.row().classes('items-center gap-2' + (' self-center' if is_short else '')):
                    _q_badge(qi)
                    relevance_badge = self._branch_relevance_badge(row_relevance)
                    oracle_click_target = relevance_badge
                    if rec is not None:
                        if oracle_click_target is None:
                            oracle_click_target = ui.button(icon='description').props('flat round dense')
                        else:
                            oracle_click_target.classes('cursor-pointer')
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
                            oracle_click_target.on('click', dlg.open)
                        except Exception:
                            pass
                    elif oracle_click_target is not None:
                        oracle_click_target.classes('cursor-default')
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
        ctx.current_query_relevance = BranchRelevance.UNKNOWN

    def _ev_path_cond(self, r, elem, ctx: 'App.QueryContext') -> None:
        if ctx.current_query_card is not None:
            raise ValueError("PathConditionAtom inside SqlQueryDecl")
        # Try to find an oracle record (conditional) for this atom via oracle_digest
        try:
            od = r.get('oracle_digest') if isinstance(r, dict) else r['oracle_digest']
        except Exception:
            od = None
        rec = ctx.oracle_by_digest.get(od) if od else None

        with ui.card().classes('w-full'):
            with ui.row().classes('items-start gap-2 w-full'):
                ui.label(f"[{int(r['event_idx'])}]").classes('text-caption text-grey')
                rel = self._branch_relevance_from_row(r)
                relevance_badge = self._branch_relevance_badge(rel)
                oracle_click_target = relevance_badge
                is_irrel = rel == BranchRelevance.IRRELEVANT
                if not is_irrel:
                    _vac_badge(r['vacuousness'])
                if rec is not None:
                    if oracle_click_target is None:
                        oracle_click_target = ui.button(icon='description').props('flat round dense')
                    else:
                        oracle_click_target.classes('cursor-pointer')
                    with ui.dialog() as dlg:
                        dlg.props('maximized no-refocus')
                        with ui.card().classes('w-screen h-screen max-w-none max-h-none p-2 flex flex-col gap-2 overflow-hidden'):
                            with ui.row().classes('items-center justify-between w-full'):
                                ui.label('Oracle Details for Condition').classes('text-subtitle1')
                                ui.button(icon='close', on_click=dlg.close).props('flat round dense')
                            with ui.element('div').classes('flex-1 min-h-0 w-full overflow-auto'):
                                self._render_oracle_record_details_content(rec)
                            with ui.row().classes('justify-end w-full'):
                                ui.button('Close', on_click=dlg.close).props('flat')
                    try:
                        oracle_click_target.on('click', dlg.open)
                    except Exception:
                        pass
                elif oracle_click_target is not None:
                    oracle_click_target.classes('cursor-default')
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
    def _compute_tokens_used_from_output(output: Optional[str]) -> Optional[int]:
        if not output:
            return None
        # Accept numbers with optional thousands separators, flexible spacing/case.
        # Handle both "tokens used: 123" and "tokens used\n123" formats
        m = re.findall(r"tokens\s*used\s*:?\s*([\d,]+)", output, flags=re.IGNORECASE)
        if not m:
            return None
        # Remove commas before converting to int
        return int(m[-1].replace(',', ''))

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

    def _load_review_state(self) -> Dict[str, dict]:
        """Load persisted oracle review state keyed by key digest."""
        path = getattr(self, 'review_state_path', None)
        if path is None:
            return {}
        try:
            with path.open('r') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        normalized: Dict[str, dict] = {}
        for k, v in data.items():
            normalized[str(k)] = self._normalize_review_entry(v)
        return normalized

    @staticmethod
    def _normalize_review_entry(value) -> Dict[str, object]:
        reviewed = False
        notes = ''
        if isinstance(value, dict):
            reviewed = bool(value.get('reviewed', False))
            notes = str(value.get('notes') or '')
        else:
            reviewed = bool(value)
        return {'reviewed': reviewed, 'notes': notes}

    def _save_review_state(self) -> None:
        """Persist the in-memory review state."""
        try:
            self.review_state_path.parent.mkdir(parents=True, exist_ok=True)
            with self.review_state_path.open('w') as f:
                json.dump(self.review_state, f, indent=2, sort_keys=True)
        except Exception:
            pass

    def _persist_review_entry(self, key_digest: str, entry: Dict[str, object]) -> None:
        """Persist a single review/notes entry; drop it if empty."""
        reviewed = bool(entry.get('reviewed', False))
        notes = str(entry.get('notes') or '')
        if not reviewed and not notes.strip():
            self.review_state.pop(key_digest, None)
        else:
            self.review_state[key_digest] = {'reviewed': reviewed, 'notes': notes}
        self._save_review_state()

    def _get_review_entry(self, key_digest: Optional[str]) -> Dict[str, object]:
        if not key_digest:
            return {'reviewed': False, 'notes': ''}
        entry = self.review_state.get(str(key_digest))
        if entry is None:
            return {'reviewed': False, 'notes': ''}
        return {'reviewed': bool(entry.get('reviewed', False)), 'notes': str(entry.get('notes') or '')}

    def _update_review_state(self, key_digest: Optional[str], reviewed: bool) -> None:
        """Update state for a specific key and flush to disk."""
        if not key_digest:
            return
        entry = self._get_review_entry(key_digest)
        entry['reviewed'] = bool(reviewed)
        self._persist_review_entry(key_digest, entry)

    def _set_note_for_digest(self, key_digest: Optional[str], note: str) -> None:
        if not key_digest:
            return
        entry = self._get_review_entry(key_digest)
        entry['notes'] = str(note or '')
        self._persist_review_entry(key_digest, entry)

    def _get_note_for_digest(self, key_digest: Optional[str]) -> str:
        if not key_digest:
            return ''
        entry = self._get_review_entry(key_digest)
        return str(entry.get('notes') or '')

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
                # Try stdout first, then stderr
                r['tokens_used'] = (
                    self._compute_tokens_used_from_output(r.get('stdout')) or
                    self._compute_tokens_used_from_output(r.get('stderr'))
                )
            # Subject normalization (query vs conditional)
            # Some datasets use 'condition' for codex-conditional; tolerate both.
            subj = r.get('query') or r.get('conditional') or r.get('condition') or ''
            r['subject'] = subj
            r['subject_preview'] = self._preview(subj)
            kd = str(r.get('key_digest', ''))
            entry = self._get_review_entry(kd)
            note_text = str(entry.get('notes') or '')
            r['reviewed'] = bool(entry.get('reviewed', False))
            r['notes'] = note_text
        return recs, chosen

    # Shared renderer for Oracle record details (used in tab and fullscreen dialog)
    def _render_oracle_record_details_content(self, rec: dict) -> None:
        """Render the Oracle record details content (metadata + sections)."""
        with ui.column().classes('gap-2 w-full min-w-0'):
            self._render_oracle_meta(rec)
            with ui.column().classes('w-full min-w-0 gap-2'):
                self._render_oracle_sql_section(rec)
                self._render_oracle_stacktrace_section(rec)
                self._render_oracle_notes_section(rec)
                self._render_oracle_report_section(rec)
                self._render_oracle_prompt_section(rec)
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

    def _render_oracle_notes_section(self, rec: dict) -> None:
        kd = str(rec.get('key_digest', ''))
        current = rec.get('notes') or self._get_note_for_digest(kd)
        with ui.expansion('Notes', icon='edit_note', value=True).classes('w-full'):
            textarea = ui.textarea(label='Notes', value=current).props('outlined autogrow').classes('w-full')
            textarea.on('change', lambda e, kd=kd, rec=rec, ta=textarea: self._on_detail_note_changed(kd, rec, ta.value))

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

    def _render_oracle_prompt_section(self, rec: dict) -> None:
        """Render the raw prompt used for this oracle record, if present."""
        if not (prompt := rec.get('prompt')):
            return
        with ui.expansion('Prompt', icon='code', value=True).classes('w-full'):
            try:
                ui.codemirror(value=str(prompt), line_wrapping=True).classes('w-full prompt-cm')
            except Exception:
                # Fallback to plain text if CodeMirror is unavailable
                self.render_terminal(str(prompt))

    def _on_detail_note_changed(self, key_digest: str, rec: dict, text: Optional[str]) -> None:
        note = '' if text is None else str(text)
        rec['notes'] = note
        self._set_note_for_digest(key_digest, note)

    @staticmethod
    def _is_conditional_record(rec: dict) -> bool:
        """Heuristic: identify conditional-oracle records.
        Considers explicit oracle kind and presence of 'conditional'/'condition' when no 'query' field.
        """
        try:
            oracle_kind = str(rec.get('oracle') or '')
        except Exception:
            oracle_kind = ''
        if oracle_kind == 'codex-conditional':
            return True
        # Treat as conditional if it carries a conditional text and lacks a query
        return (('conditional' in rec or 'condition' in rec) and not rec.get('query'))

    @staticmethod
    def _is_query_record(rec: dict) -> bool:
        """Records that are not conditionals are considered query-oriented."""
        return not App._is_conditional_record(rec)

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

            # Split view: a tab for Queries and one for Conditionals.
            self._oracle_tabs(records)

    def _oracle_tabs(self, records: List[dict]) -> None:
        queries = [r for r in records if self._is_query_record(r)]
        conds = [r for r in records if self._is_conditional_record(r)]

        with ui.tabs() as tabs:
            t_q = ui.tab('Queries')
            t_c = ui.tab('Conditionals')

        with ui.tab_panels(tabs, value=t_q).classes('w-full flex-1 min-h-0'):
            with ui.tab_panel(t_q):
                # Chart
                self._oracle_summary_chart(queries)
                ui.separator()
                # Grid
                with ui.element('div').classes('w-full flex-1 min-h-0'):
                    grid_q, by_q = self._oracle_grid(queries, dom_id='oracle-grid-queries', subject_label='Query')
                    self._attach_reviewed_listener(grid_q, by_q)
            with ui.tab_panel(t_c):
                # Chart
                self._oracle_summary_chart(conds)
                ui.separator()
                # Grid
                with ui.element('div').classes('w-full flex-1 min-h-0'):
                    grid_c, by_c = self._oracle_grid(conds, dom_id='oracle-grid-conditionals', subject_label='Condition')
                    self._attach_reviewed_listener(grid_c, by_c)

        # Shared record details dialog; hook both grids
        rec_dlg, rec_dlg_container = self._oracle_record_dialog()
        grid_q.on('rowDoubleClicked', lambda e: self._on_oracle_row_double_clicked(e, by_q, rec_dlg, rec_dlg_container), args=['data'])
        grid_c.on('rowDoubleClicked', lambda e: self._on_oracle_row_double_clicked(e, by_c, rec_dlg, rec_dlg_container), args=['data'])

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

    def _oracle_summary_charts_split(self, records: List[dict]) -> None:
        """Render two compact stacked bar charts: Queries vs Conditionals.
        Falls back gracefully if one category is empty.
        """
        queries = [r for r in records if self._is_query_record(r)]
        conds = [r for r in records if self._is_conditional_record(r)]

        with ui.row().classes('items-start gap-6 flex-wrap w-full'):
            with ui.column().classes('gap-1 min-w-[280px] flex-1'):
                ui.label('Queries').classes('text-caption text-grey')
                self._oracle_summary_chart(queries)
            with ui.column().classes('gap-1 min-w-[280px] flex-1'):
                ui.label('Conditionals').classes('text-caption text-grey')
                self._oracle_summary_chart(conds)

    def _oracle_grid(self, records: List[dict], *, dom_id: Optional[str] = None, subject_label: str = 'Subject'):
        grid = ui.aggrid({
            'defaultColDef': {'resizable': True, 'sortable': True},
            'animateRows': True,
            'ensureDomOrder': True,
            'suppressCellFocus': True,
            'suppressMovableColumns': True,
            'tooltipShowDelay': 300,
            'rowModelType': 'clientSide',
            'suppressHorizontalScroll': False,
            'alwaysShowHorizontalScroll': False,
            'alwaysShowVerticalScroll': False,
            'columnDefs': [
                {'headerName': '#', 'field': 'row_idx', 'width': 70, 'minWidth': 60, 'maxWidth': 90, 'pinned': 'left', 'sortable': False, 'resizable': False, 'suppressMenu': True, 'type': 'rightAligned', 'cellClass': 'text-right text-grey'},
                {
                    'headerName': 'Reviewed',
                    'field': 'reviewed',
                    'width': 120,
                    'minWidth': 110,
                    'maxWidth': 140,
                    'cellRenderer': 'agCheckboxCellRenderer',
                    'cellEditor': 'agCheckboxCellEditor',
                    'editable': True,
                    'pinned': 'left',
                    'sortable': False,
                    'resizable': False,
                    'suppressMenu': True,
                    'cellClass': 'text-center',
                },
                {
                    'headerName': subject_label,
                    'field': 'subject',
                    'filter': True,
                    'tooltipField': 'subject',
                    'flex': 2,
                    'minWidth': 320,
                    'cellStyle': { 'fontFamily': 'monospace' },
                },
                {
                    'headerName': 'Verdict',
                    'field': 'verdict',
                    'width': 110, 'minWidth': 100,
                    'cellClassRules': {
                        'text-positive': "data.verdict === 'Relevant'",
                        'text-negative': "data.verdict === 'Irrelevant'",
                        'text-warning': "data.verdict === 'Unsure'",
                        'text-gray-700': "data.verdict === 'Unknown'"
                    }
                },
                {
                    'headerName': 'Duration (s)',
                    'field': 'dur_s',
                    'width': 110, 'minWidth': 100,
                    'type': 'numericColumn',
                    'valueFormatter': 'value != null ? value.toFixed(2) : ""',
                },
                {'headerName': 'Tokens', 'field': 'tokens_used', 'width': 140, 'minWidth': 120, 'type': 'numericColumn'},
                {'headerName': 'Exit', 'field': 'exit_code', 'width': 80, 'minWidth': 70, 'type': 'numericColumn'},
            ],
            'rowData': records,
            'rowSelection': 'single',
            'rowClassRules': {
                'oracle-row-reviewed': 'data.reviewed === true',
            },
        })
        if dom_id:
            grid.props(f'id={dom_id}')
        grid.classes('oracle-grid w-full h-full max-w-none').style('height: 100%')
        by_digest: Dict[str, dict] = {r['key_digest']: r for r in records}
        return grid, by_digest

    def _attach_reviewed_listener(self, grid, by_digest: Dict[str, dict]) -> None:
        """Attach a cell-change listener to persist the Reviewed checkbox state."""
        try:
            grid.on('cellValueChanged', lambda e: self._on_oracle_reviewed_changed(e, by_digest))
        except Exception:
            pass

    def _on_oracle_reviewed_changed(self, event, by_digest: Dict[str, dict]) -> None:
        args = getattr(event, 'args', None)
        if not isinstance(args, dict):
            return
        # Ensure we only react to the Reviewed column edits
        col = args.get('colId') or args.get('field')
        if col != 'reviewed':
            return
        data = args.get('data')
        if not isinstance(data, dict):
            return
        kd = data.get('key_digest')
        if not kd:
            return

        new_value = args.get('newValue')
        reviewed = self._coerce_to_bool(new_value)
        if kd in by_digest:
            by_digest[kd]['reviewed'] = reviewed
        self._update_review_state(kd, reviewed)

    @staticmethod
    def _coerce_to_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {'1', 'true', 'yes', 'on'}
        if isinstance(value, (int, float)):
            return value != 0
        return bool(value)

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
    # Allow zero files - the UI will handle this gracefully by showing no data
    return p.expanduser()
