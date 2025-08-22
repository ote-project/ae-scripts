#!/usr/bin/env python3
import argparse
import collections
from dataclasses import dataclass
from typing import Dict, Iterable, Any, Tuple

import streamlit as st
import sqlparse
import pandas as pd
import numpy as np

# Reuse helpers and enums from the existing viewer to keep consistency.
from view_query_relevance_report import (
    Verdict,
    read_jsonl,
    preview,
    get_verdict_markdown_badge,
    compute_verdict,
    generate_prompt,
)


@dataclass(frozen=True)
class Key:
    query: str
    stacktrace: Tuple[str, ...]


def parse_args():
    p = argparse.ArgumentParser(description="Diff two query relevance JSONL files")
    p.add_argument("left_file", help="Path to the first JSONL file (left)")
    p.add_argument("right_file", help="Path to the second JSONL file (right)")
    return p.parse_args()


def load_records(path: str) -> list[dict]:
    try:
        with open(path, "r") as f:
            return list(read_jsonl(f))
    except FileNotFoundError:
        st.error(f"File not found: {path}")
        st.stop()
    except Exception as e:
        st.error(f"Error reading file {path}: {e}")
        st.stop()


def key_of(rec: Dict[str, Any]) -> Key | None:
    query = rec.get("query")
    stack = rec.get("stacktrace")
    if not isinstance(query, str) or not isinstance(stack, list) or not all(isinstance(x, str) for x in stack):
        return None
    return Key(query=query, stacktrace=tuple(stack))


def index_records(records: Iterable[dict]) -> Dict[Key, dict]:
    idx: Dict[Key, dict] = {}
    for rec in records:
        k = key_of(rec)
        if k is None:
            continue
        # Normalize verdicts for consistent downstream usage
        rec["verdict"] = compute_verdict(rec)
        # Keep the last occurrence if duplicates appear
        idx[k] = rec
    return idx


def make_change_label(lv: Verdict, rv: Verdict) -> str:
    return f"{lv.value} → {rv.value}"


def main() -> None:
    args = parse_args()

    left_records = load_records(args.left_file)
    right_records = load_records(args.right_file)

    if not left_records:
        st.warning(f"No records were found in {args.left_file}")
        st.stop()
    if not right_records:
        st.warning(f"No records were found in {args.right_file}")
        st.stop()

    left_idx = index_records(left_records)
    right_idx = index_records(right_records)

    left_keys = set(left_idx.keys())
    right_keys = set(right_idx.keys())
    common_keys = left_keys & right_keys

    # We'll compute performance (speedups) after reading any filters
    # so that the chart respects the current filter.
    speedups: list[float] = []

    # Only show pairs with different verdicts
    diffs = []
    change_counter = collections.Counter()
    for k in sorted(common_keys, key=lambda kk: (kk.query, kk.stacktrace)):
        lrec = left_idx[k]
        rrec = right_idx[k]
        lv = lrec.get("verdict", Verdict.UNKNOWN)
        rv = rrec.get("verdict", Verdict.UNKNOWN)
        if lv != rv:
            diffs.append((k, lrec, rrec))
            change_counter[(lv, rv)] += 1

    # ---------- page layout -------------------------------------------------
    st.set_page_config(page_title="Query Diff Viewer", layout="wide")
    st.title("Query Relevance Diff Viewer")
    st.caption(
        f"Comparing left: {args.left_file} vs right: {args.right_file}.\n"
        f"Left records: {len(left_records)} • Right records: {len(right_records)} • Common: {len(common_keys)} • Diffs: {len(diffs)}"
    )

    # Summary metrics
    colA, colB, colC, colD = st.columns(4)
    colA.metric("Common keys", len(common_keys))
    colB.metric("Changed verdicts", len(diffs))
    colC.metric("Only in left", len(left_keys - right_keys))
    colD.metric("Only in right", len(right_keys - left_keys))

    # Optional: filter by search term
    with st.expander("Filters", expanded=False):
        search = st.text_input("Search in query", value="")
        if search:
            diffs = [t for t in diffs if search.lower() in t[0].query.lower()]

    # Recompute performance metrics (speedups) from filtered keys
    # Speedup S = left_time / right_time (>1 means faster, <1 means slower)
    if search:
        filtered_keys_for_perf = [k for k in common_keys if search.lower() in k.query.lower()]
    else:
        filtered_keys_for_perf = list(common_keys)

    for k in filtered_keys_for_perf:
        lrec = left_idx[k]
        rrec = right_idx[k]
        left_dur = lrec.get("dur_s")
        right_dur = rrec.get("dur_s")
        if (
            isinstance(left_dur, (int, float))
            and isinstance(right_dur, (int, float))
            and left_dur > 0
            and right_dur > 0
        ):
            speedup = left_dur / right_dur
            speedups.append(speedup)

    st.subheader("Changed Verdicts")
    if not diffs:
        st.info("No verdict differences found.")
    else:
        # Quick list of change counts
        if change_counter:
            st.write("Change summary:")
            cols = st.columns(min(4, len(change_counter)))
            for (i, ((lv, rv), count)) in enumerate(change_counter.most_common()):
                with cols[i % len(cols)]:
                    st.metric(make_change_label(lv, rv), count)

        st.divider()

        # Detailed view per diff
        for i, (k, lrec, rrec) in enumerate(diffs, start=1):
            header = f"#{i}: {preview(k.query, 120)}"
            with st.expander(header):
                # Verdicts and core metadata
                lc, rc = st.columns(2)
                with lc:
                    st.write("**Left verdict:**")
                    st.markdown(get_verdict_markdown_badge(lrec.get("verdict", Verdict.UNKNOWN)))
                with rc:
                    st.write("**Right verdict:**")
                    st.markdown(get_verdict_markdown_badge(rrec.get("verdict", Verdict.UNKNOWN)))

                st.write("**Query**")
                st.code(sqlparse.format(k.query, reindent=True, keyword_case="upper"), language="sql")

                st.write("**Stack trace**")
                # Render a scrollable code-like box for long traces, preserving per-frame lines
                st.code(
                    "\n".join(k.stacktrace),
                    language="text",
                    line_numbers=True,
                )

                tabs = st.tabs(["Report", "stdout", "stderr", "Original prompt", "Current prompt"])
                # Reports side-by-side
                with tabs[0]:
                    c1, c2 = st.columns(2)
                    with c1:
                        st.caption("Left: report")
                        st.markdown(lrec.get("last_message", ""))
                    with c2:
                        st.caption("Right: report")
                        st.markdown(rrec.get("last_message", ""))

                with tabs[1]:
                    c1, c2 = st.columns(2)
                    with c1:
                        st.caption("Left: stdout")
                        st.code(lrec.get("stdout", ""))
                    with c2:
                        st.caption("Right: stdout")
                        st.code(rrec.get("stdout", ""))

                with tabs[2]:
                    c1, c2 = st.columns(2)
                    with c1:
                        st.caption("Left: stderr")
                        st.code(lrec.get("stderr", ""))
                    with c2:
                        st.caption("Right: stderr")
                        st.code(rrec.get("stderr", ""))

                with tabs[3]:
                    c1, c2 = st.columns(2)
                    with c1:
                        st.caption("Left: original prompt")
                        left_prompt = lrec.get("prompt", "")
                        if left_prompt:
                            st.code(left_prompt, language="text")
                        else:
                            st.info("No original prompt found in left record")
                    with c2:
                        st.caption("Right: original prompt")
                        right_prompt = rrec.get("prompt", "")
                        if right_prompt:
                            st.code(right_prompt, language="text")
                        else:
                            st.info("No original prompt found in right record")

                with tabs[4]:
                    st.warning(
                        ":material/warning: The prompt shown below is constructed from the **current** template, "
                        "which may have been updated since these records were generated."
                    )
                    prompt = generate_prompt(k.query, k.stacktrace)
                    st.code(prompt, language="text", line_numbers=True)

    # Performance comparison (moved after Changed verdicts)
    if speedups:
        st.subheader("Performance Comparison")
        # Indicate that performance is filtered when a search is active
        if search:
            st.caption(
                f"Performance metrics reflect the filtered subset: "
                f"{len(filtered_keys_for_perf)} matching common queries."
            )
        
        # Summary statistics
        speedup_array = np.array(speedups)
        faster_count = int(np.sum(speedup_array > 1))
        slower_count = int(np.sum(speedup_array < 1))

        col1, col2, col3 = st.columns(3)
        col1.metric("Faster queries", f"{faster_count} ({100*faster_count/len(speedups):.1f}%)")
        col2.metric("Slower queries", f"{slower_count} ({100*slower_count/len(speedups):.1f}%)")
        # Geometric mean is the standard aggregate for multiplicative ratios like speedups
        valid_speedups = speedup_array[np.isfinite(speedup_array) & (speedup_array > 0)]
        if valid_speedups.size > 0:
            geom_mean = float(np.exp(np.mean(np.log(valid_speedups))))
            col3.metric("Geometric mean speedup", f"{geom_mean:.2f}x")
        else:
            col3.metric("Geometric mean speedup", "N/A")

        # Empirical CDF of speedups
        xs = np.sort(np.array(speedups, dtype=float))
        if xs.size > 0:
            cdf = np.arange(1, xs.size + 1, dtype=float) / xs.size
        else:
            cdf = np.array([], dtype=float)

        df_ecdf = pd.DataFrame({
            "speedup": xs,
            "percent": 100.0 * cdf,
        })

        st.caption("Empirical CDF of speedups")
        use_log_scale = st.toggle("Log-scale x-axis", value=False)
        x_scale = {"type": "log"} if use_log_scale else {"type": "linear"}

        spec_ecdf = {
            "layer": [
                {
                    "transform": [{"calculate": "'ECDF'", "as": "series"}],
                    "mark": {"type": "line"},
                    "encoding": {
                        "x": {
                            "field": "speedup",
                            "type": "quantitative",
                            "axis": {"title": "Speedup (left_time / right_time)"},
                            "scale": x_scale,
                        },
                        "y": {
                            "field": "percent",
                            "type": "quantitative",
                            "axis": {"title": "Queries ≤ x (%)"},
                            "scale": {"domain": [0, 100]},
                        },
                        "color": {
                            "field": "series",
                            "type": "nominal",
                            "legend": {"title": ""},
                            "scale": {"domain": ["ECDF", "No change", "Geometric mean"], "range": ["steelblue", "red", "orange"]},
                        },
                        "strokeDash": {
                            "field": "series",
                            "type": "nominal",
                            "scale": {"domain": ["ECDF", "No change", "Geometric mean"], "range": [[], [6, 4], []]},
                        },
                        "tooltip": [
                            {"field": "speedup", "type": "quantitative", "title": "Speedup", "format": ".2f"},
                            {"field": "percent", "type": "quantitative", "title": "ECDF (%)", "format": ".1f"},
                        ],
                    },
                },
                {
                    "data": {"values": [{"x": 1.0, "series": "No change"}]},
                    "mark": {"type": "rule"},
                    "encoding": {
                        "x": {"field": "x", "type": "quantitative", "scale": x_scale},
                        "color": {
                            "field": "series",
                            "type": "nominal",
                            "legend": {"title": ""},
                            "scale": {"domain": ["ECDF", "No change", "Geometric mean"], "range": ["steelblue", "red", "orange"]},
                        },
                        "strokeDash": {
                            "field": "series",
                            "type": "nominal",
                            "scale": {"domain": ["ECDF", "No change", "Geometric mean"], "range": [[], [6, 4], []]},
                        },
                        "tooltip": [{"field": "x", "type": "quantitative", "title": "No change", "format": ".2f"}],
                    },
                },
            ]
        }

        # Add geometric mean vertical line and legend entry when available
        if valid_speedups.size > 0:
            geom_plot = float(np.exp(np.mean(np.log(valid_speedups))))
            spec_ecdf["layer"].append(
                {
                    "data": {"values": [{"x": geom_plot, "series": "Geometric mean"}]},
                    "mark": {"type": "rule"},
                    "encoding": {
                        "x": {"field": "x", "type": "quantitative", "scale": x_scale},
                        "color": {
                            "field": "series",
                            "type": "nominal",
                            "legend": {"title": ""},
                            "scale": {"domain": ["ECDF", "No change", "Geometric mean"], "range": ["steelblue", "red", "orange"]},
                        },
                        "strokeDash": {
                            "field": "series",
                            "type": "nominal",
                            "scale": {"domain": ["ECDF", "No change", "Geometric mean"], "range": [[], [6, 4], []]},
                        },
                        "tooltip": [
                            {"field": "x", "type": "quantitative", "title": "Geometric mean", "format": ".2f"}
                        ],
                    },
                }
            )

        st.vega_lite_chart(df_ecdf, spec_ecdf, use_container_width=True)
    else:
        if search:
            st.warning(
                f"No duration data found for performance comparison in the filtered subset "
                f"({len(filtered_keys_for_perf)} matching common queries)."
            )
        else:
            st.warning("No duration data found for performance comparison.")


if __name__ == "__main__":
    main()
