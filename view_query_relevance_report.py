import argparse
import collections
from enum import Enum
import json
import re
import html
from typing import Iterable, Dict, Any

import pandas as pd
import sqlparse
import streamlit as st

from analyze_query_relevance import PROMPT_TEMPLATE


def strip_leading_non_alnum(s):
    return re.sub(r'^[^a-zA-Z0-9]+', '', s)


class Verdict(Enum):
    RELEVANT = "Relevant"
    IRRELEVANT = "Irrelevant"
    UNSURE = "Unsure"
    UNKNOWN = "Unknown"

    @staticmethod
    def of(verdict: str | None) -> "Verdict":
        """Convert a string to a Verdict enum."""
        if verdict is None:
            return Verdict.UNKNOWN

        match verdict.lower():
            case "yes" | "relevant":
                return Verdict.RELEVANT
            case "no" | "irrelevant":
                return Verdict.IRRELEVANT
            case "unsure":
                return Verdict.UNSURE
            case _:
                raise ValueError(f"Unknown verdict: {verdict}")


# ---------- helpers ---------------------------------------------------------
def read_jsonl(fp) -> Iterable[Dict[str, Any]]:
    """Yield one dict per non-blank line read from the file-like object `fp`."""
    for line in fp:
        line = line.strip()
        if line:
            yield json.loads(line)


def preview(text: str | None, length: int = 80) -> str:
    """Return a short, single-line preview with ellipsis if the text is long."""
    if text is None:
        return ""
    return text if len(text) <= length else text[: length - 1] + "…"


def get_verdict_badge_emoji(verdict: Verdict) -> str:
    """Get a colored badge for the verdict using emoji that work in DataFrames."""
    match verdict:
        case Verdict.RELEVANT:
            return "✅ Relevant"
        case Verdict.IRRELEVANT:
            return "❌ Irrelevant"
        case Verdict.UNSURE:
            return "❓ Unsure"
        case Verdict.UNKNOWN:
            return "❔ Unknown"


def get_verdict_markdown_badge(verdict: Verdict) -> str:
    """Get a colored badge for the verdict using Streamlit markdown badges."""
    match verdict:
        case Verdict.RELEVANT:
            return f":green-badge[:material/check_circle: Relevant]"
        case Verdict.IRRELEVANT:
            return f":red-badge[:material/block: Irrelevant]"
        case Verdict.UNSURE:
            return f":violet-badge[:material/help: Unsure]"
        case Verdict.UNKNOWN:
            return f":orange-badge[:material/help: Unknown]"


def generate_prompt(query: str, stacktrace: list) -> str:
    """Generate the prompt for a given query and stacktrace."""
    return PROMPT_TEMPLATE.format(
        query=query,
        stacktrace="\n".join(stacktrace)
    )


def parse_args():
    parser = argparse.ArgumentParser(description="View query relevance report from JSONL file")
    parser.add_argument("input_file", help="Path to the JSONL file to read")
    return parser.parse_args()


def compute_verdict(rec: dict) -> Verdict:
    """
    Determine the verdict for a given record.

    This function attempts to extract a verdict from the record dictionary `rec`.
    It first checks if the "verdict" field is present and, if so, converts it to a `Verdict` enum.
    If not, it looks for a "last_message" field and parses its lines to find a verdict
    by checking for lines that start with "RELEVANT", "IRRELEVANT", or "UNSURE".
    If multiple conflicting verdicts are found in the message, or if no verdict can be determined,
    it returns `Verdict.UNKNOWN`.

    Args:
        rec (dict): The record containing verdict information.

    Returns:
        Verdict: The determined verdict as a `Verdict` enum.
    """
    if (verdict_str := rec.get("verdict")) is not None:
        return Verdict.of(verdict_str)
    
    if (last_message := rec.get("last_message")) is None:
        return Verdict.UNKNOWN
    
    final_verdict = Verdict.UNKNOWN
    for line in last_message.splitlines():
        line = strip_leading_non_alnum(line)
        this_verdict = Verdict.UNKNOWN
        if line.startswith("RELEVANT"):
            this_verdict = Verdict.RELEVANT
        elif line.startswith("IRRELEVANT"):
            this_verdict = Verdict.IRRELEVANT
        elif line.startswith("UNSURE"):
            this_verdict = Verdict.UNSURE

        if this_verdict != Verdict.UNKNOWN:
            if final_verdict == Verdict.UNKNOWN:
                final_verdict = this_verdict
            elif final_verdict != this_verdict:
                # Multiple verdicts found.
                return Verdict.UNKNOWN

    return final_verdict

def main() -> None:
    args = parse_args()

    try:
        with open(args.input_file, 'r') as f:
            records = list(read_jsonl(f))
    except FileNotFoundError:
        st.error(f"File not found: {args.input_file}")
        st.stop()
    except Exception as e:
        st.error(f"Error reading file {args.input_file}: {e}")
        st.stop()

    if not records:
        st.warning(f"No records were found in {args.input_file}")
        st.stop()

    # Normalize verdicts.
    for rec in records:
        rec["verdict"] = compute_verdict(rec)

    # ---------- page layout -----------------------------------------------------
    st.set_page_config(page_title="Query Results Viewer", layout="wide")
    st.title("Query Results Viewer")
    st.caption(f"Loaded **{len(records)}** record(s) from {args.input_file}.")

    st.subheader("Summary")

    # Top-level metrics summary
    counts = collections.Counter(r["verdict"] for r in records)
    for col, verdict in zip(st.columns(4), (Verdict.RELEVANT, Verdict.IRRELEVANT, Verdict.UNSURE, Verdict.UNKNOWN)):
        col.metric(get_verdict_markdown_badge(verdict), counts.get(verdict, 0))

    # ---------- summary table ---------------------------------------------------
    summary_df = pd.DataFrame(
        {
            "Query": [preview(r["query"], 120) for r in records],
            "Verdict": [get_verdict_badge_emoji(r.get("verdict")) for r in records],
            "Duration (s)": [r.get("dur_s") for r in records],
            "Tokens used": [r.get("tokens_used") for r in records],
            "Exit code": [r.get("exit_code") for r in records],
        }
    )

    st.dataframe(summary_df, height=min(400, 40 + 35 * len(summary_df)), use_container_width=True)

    st.divider()

    # ---------- detailed, expandable view --------------------------------------
    st.subheader("Detailed records")

    for idx, rec in enumerate(records):
        with st.expander(f"**Record #{idx}:** {preview(rec['query'], 120)}"):
            # Core metadata columns
            col1, col2, col3, col4 = st.columns(4)

            with col1:
                st.write("**Verdict:**")
                st.markdown(get_verdict_markdown_badge(rec.get("verdict")))

            with col2:
                st.write("**Tokens used:**")
                st.write(rec.get("tokens_used", "—"))

            with col3:
                st.write("**Duration (s):**")
                st.write(rec.get("dur_s", "—"))

            with col4:
                st.write("**Exit code:**")
                st.write(rec.get("exit_code", "—"))

            tabs = st.tabs(["SQL", "Stack trace", "Report", "stdout", "stderr", "Copy prompt"])
            with tabs[0]:
                st.code(
                    sqlparse.format(rec["query"], reindent=True, keyword_case="upper"),
                    language="sql",
                    line_numbers=True,
                )
            with tabs[1]:
                # Render stacktrace in a scrollable, fixed-height mono box with per-frame lines
                safe_lines = [html.escape(frame) for frame in rec["stacktrace"]]
                html_lines = "<br/>".join(safe_lines)
                st.markdown(
                    (
                        "<div style='max-height: 320px; overflow-y: auto; border: 1px solid #ddd; "
                        "border-radius: 4px; padding: 8px;'>"
                        "<code style='display:block; white-space: pre; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, Liberation Mono, monospace;'>"
                        f"{html_lines}"
                        "</code></div>"
                    ),
                    unsafe_allow_html=True,
                )
            with tabs[2]:
                st.markdown(rec["last_message"])
            with tabs[3]:
                st.code(rec["stdout"])
            with tabs[4]:
                st.code(rec["stderr"])
            with tabs[5]:
                st.warning(
                    ":material/warning: The prompt shown below is constructed from the **current** template, "
                    "which may have been updated since this record was generated."
                )
                prompt = generate_prompt(rec["query"], rec["stacktrace"])
                st.code(prompt, language="text", line_numbers=True)


if __name__ == "__main__":
    main()
