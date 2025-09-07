from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
from nicegui import ui


# Candidate series columns to plot, ordered by preference
RateCols = ['m1_rate', 'mean_rate', 'm5_rate', 'm15_rate', 'count']
DurCols = ['max', 'mean', 'min', 'stddev', 'p50', 'p75', 'p95', 'p98', 'p99', 'p999']


def _list_metric_files(metrics_dir: Path) -> List[Path]:
    try:
        return sorted([p for p in metrics_dir.glob('*.csv') if p.is_file()])
    except Exception:
        return []


def _read_header(fp: Path) -> List[str]:
    try:
        df = pd.read_csv(fp, nrows=0)
        return list(df.columns)
    except Exception:
        return []


def _friendly_name(fp: Path) -> str:
    name = fp.stem
    prefix = 'edu.berkeley.cs.netsys.policy_extraction.'
    if name.startswith(prefix):
        name = name[len(prefix) :]
    return name


def _best_rate_column(columns: Iterable[str]) -> Optional[str]:
    for c in RateCols:
        if c in columns:
            return c
    return None


def _best_duration_column(columns: Iterable[str]) -> Optional[str]:
    for c in DurCols:
        if c in columns:
            return c
    return None


def _chart_title(base: str, ycol: str) -> str:
    pretty_y = 'm\u2081_rate' if ycol == 'm1_rate' else ycol
    return f'{base} — {pretty_y}'


def _read_units(fp: Path) -> tuple[Optional[str], Optional[str]]:
    """Return (rate_unit, duration_unit) if present in the CSV; else (None, None)."""
    try:
        df = pd.read_csv(fp, usecols=['rate_unit', 'duration_unit'], nrows=1)
        rate_u = str(df['rate_unit'].iloc[0]) if 'rate_unit' in df.columns else None
        dur_u = str(df['duration_unit'].iloc[0]) if 'duration_unit' in df.columns else None
        return rate_u, dur_u
    except Exception:
        return None, None


def _draw_chart(fp: Path, ycol: str) -> None:
    try:
        df = pd.read_csv(fp, usecols=[c for c in ['t', ycol] if c is not None])
    except Exception as e:
        ui.label(f'Failed to read metrics CSV: {e}').classes('text-negative')
        return

    if not {'t', ycol}.issubset(df.columns):
        ui.label(f"Metrics CSV missing required columns 't' and '{ycol}'.").classes('text-negative')
        return

    try:
        df = df[['t', ycol]].dropna().sort_values('t')
        t0 = float(df['t'].iloc[0]) if len(df) else 0.0
        df['t_rel'] = df['t'].astype(float) - t0
        points: List[List[float]] = [[float(r['t_rel']), float(r[ycol])] for _, r in df.iterrows()]
    except Exception as e:
        ui.label(f'Failed to process metrics data: {e}').classes('text-negative')
        return

    if not points:
        ui.label('No data points to display.')
        return

    try:
        t_rel_max = float(df['t_rel'].iloc[-1]) if len(df) else 0.0
    except Exception:
        t_rel_max = 0.0

    unit = 's'
    factor = 1.0
    if t_rel_max >= 3600:
        unit, factor = 'h', 3600.0
    elif t_rel_max >= 300:
        unit, factor = 'min', 60.0

    # Prepare base arrays for dynamic y-scaling
    x_scaled = [p[0] / factor for p in points]
    y_base = [p[1] for p in points]

    rate_unit, duration_unit = _read_units(fp)

    # Duration auto-scaling helper
    def choose_duration_scale(max_val: float, base_unit: Optional[str]) -> tuple[str, float]:
        u = (base_unit or '').lower()
        if u in {'ms', 'millisecond', 'milliseconds'}:
            if max_val >= 60000:
                return 'minutes', 60000.0
            if max_val >= 1000:
                return 'seconds', 1000.0
            return 'milliseconds', 1.0
        if u in {'us', 'microsecond', 'microseconds'}:
            if max_val >= 60_000_000:
                return 'minutes', 60_000_000.0
            if max_val >= 1_000_000:
                return 'seconds', 1_000_000.0
            if max_val >= 1_000:
                return 'milliseconds', 1_000.0
            return 'microseconds', 1.0
        if u in {'ns', 'nanosecond', 'nanoseconds'}:
            if max_val >= 60_000_000_000:
                return 'minutes', 60_000_000_000.0
            if max_val >= 1_000_000_000:
                return 'seconds', 1_000_000_000.0
            if max_val >= 1_000_000:
                return 'milliseconds', 1_000_000.0
            if max_val >= 1_000:
                return 'microseconds', 1_000.0
            return 'nanoseconds', 1.0
        if u in {'s', 'sec', 'secs', 'second', 'seconds'}:
            if max_val >= 3600:
                return 'hours', 3600.0
            if max_val >= 60:
                return 'minutes', 60.0
            return 'seconds', 1.0
        return (base_unit or ''), 1.0

    # Choose y-axis label and scaling based on series type
    y_name = ycol
    y_scale = 1.0
    if ycol == 'count':
        y_name = 'count'
    elif ycol in RateCols:
        y_name = f"{ycol} ({rate_unit})" if (rate_unit and 'rate' in ycol) else (f"{ycol} (events/s)" if 'rate' in ycol else ycol)
    elif ycol in DurCols:
        max_val = float(max(y_base)) if y_base else 0.0
        scaled_unit, y_scale = choose_duration_scale(max_val, duration_unit)
        y_name = f"{ycol} ({scaled_unit})" if scaled_unit else ycol

    chart = ui.echart(
        {
            'animation': False,
            'title': {
                'text': _chart_title(_friendly_name(fp), ycol),
                'left': 'center',
                'top': 6,
                'textStyle': {'fontSize': 14, 'fontWeight': 500},
            },
            'tooltip': {'trigger': 'axis', 'appendToBody': True},
            'toolbox': {
                'right': 8,
                'feature': {
                    'dataZoom': {'yAxisIndex': 'none'},
                    'restore': {},
                },
            },
            'dataZoom': [
                {'type': 'inside', 'xAxisIndex': [0]},
                {'type': 'slider', 'xAxisIndex': [0], 'height': 18, 'bottom': 6},
            ],
            'grid': {'left': 36, 'right': 16, 'top': 48, 'bottom': 44, 'containLabel': True},
            'xAxis': {
                'type': 'value',
                'name': f't ({unit})',
                'nameLocation': 'middle',
                'nameGap': 28,
                'axisLabel': {'color': '#666', 'formatter': f'{{value}} {unit}'},
                'splitLine': {'show': True, 'lineStyle': {'color': '#eee'}},
            },
            'yAxis': {
                'type': 'value',
                'name': y_name,
                'nameLocation': 'middle',
                'nameGap': 40,
                'axisLabel': {'color': '#666'},
                'splitLine': {'show': True, 'lineStyle': {'color': '#eee'}},
            },
            'series': [
                {
                    'name': ycol,
                    'type': 'line',
                    'showSymbol': False,
                    'smooth': False,
                    'data': [[x, (y / y_scale) if ycol in DurCols else y] for x, y in zip(x_scaled, y_base)],
                    'lineStyle': {'width': 2},
                }
            ],
        }
    ).classes('w-full').style('height: 380px')

    # Note: dynamic Y rescaling on zoom intentionally omitted for simplicity


def render_metrics_tab(run_dir: Path) -> None:
    """Render the Metrics tab with selectable metrics and series.

    - Scans `<run_dir>/metrics/*.csv`
    - Lists files that contain `t` and at least one of rate or duration columns
      (rates: m1_rate, mean_rate, m5_rate, m15_rate, count; durations: max, mean, min, stddev, p50, p75, p95, p98, p99, p999)
    - Default selection prefers the precise `ConcolicSolver.num_paths_added.csv`
    - Plots the selected series (prefers m1_rate; else best duration) vs relative time
    """
    metrics_dir = run_dir / 'metrics'
    if not metrics_dir.is_dir():
        ui.label('Metrics directory not found under the run directory.').classes('text-negative')
        return

    files = _list_metric_files(metrics_dir)
    valid: List[Tuple[str, Path, List[str]]] = []
    for fp in files:
        cols = _read_header(fp)
        if 't' in cols and (_best_rate_column(cols) or _best_duration_column(cols)):
            valid.append((_friendly_name(fp), fp, cols))

    if not valid:
        ui.label('No compatible metrics CSVs found (need t + rate columns).').classes('text-negative')
        return

    preferred_suffix = 'solver.ConcolicSolver.num_paths_added.csv'
    default_idx = next((i for i, (_, fp, _) in enumerate(valid) if str(fp).endswith(preferred_suffix)), 0)
    default_label, default_fp, default_cols = valid[default_idx]
    preferred_series_order = ['m1_rate', 'mean', 'p50', 'count']
    default_y = next((c for c in preferred_series_order if c in default_cols), None) or _best_rate_column(default_cols) or _best_duration_column(default_cols) or 'm1_rate'

    with ui.row().classes('items-end gap-4 flex-wrap'):
        # NiceGUI select expects options as {value: label}
        file_options: Dict[str, str] = {str(fp): label for label, fp, _ in valid}
        sel_file = ui.select(file_options, value=str(default_fp), label='Metric').props('dense')

        def series_options(cols: List[str]) -> Dict[str, str]:
            ordered: List[str] = []
            for c in ['m1_rate', 'mean', 'p50', 'count']:
                if c in cols and c not in ordered:
                    ordered.append(c)
            for c in (RateCols + DurCols):
                if c in cols and c not in ordered:
                    ordered.append(c)
            return {c: c for c in ordered}

        sel_series = ui.select(series_options(default_cols), value=default_y, label='Series').props('dense')

    chart_container = ui.element('div').classes('w-full')

    def redraw():
        chart_container.clear()
        with chart_container:
            fp = Path(sel_file.value)
            ycol = str(sel_series.value)
            _draw_chart(fp, ycol)

    def on_file_change(e=None):
        try:
            path = Path(sel_file.value)
            cols = _read_header(path)
            new_y = next((c for c in preferred_series_order if c in cols), None) or _best_rate_column(cols) or _best_duration_column(cols) or default_y
            sel_series.options = series_options(cols)
            sel_series.value = new_y
            sel_series.update()
        except Exception:
            pass
        redraw()

    def on_series_change(e=None):
        redraw()

    sel_file.on_value_change(on_file_change)
    sel_series.on_value_change(on_series_change)

    redraw()
