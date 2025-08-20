import argparse
import collections
import json
import re
from enum import Enum
from typing import Any, Iterable

import pandas as pd
import sqlparse
import streamlit as st

from analyze_query_relevance import PROMPT_TEMPLATE


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
def read_jsonl(fp) -> Iterable[dict[str, Any]]:
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


def compute_tokens_used_from_stdout(stdout: str | None) -> int | None:
    """Extract the last "tokens used: N" occurrence from stdout, if present."""
    if not stdout:
        return None
    matches = re.findall(r"tokens used: (\d+)", stdout)
    return int(matches[-1]) if matches else None


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
    Determine the verdict for a given record, computing one if not present.
    """
    if (verdict_str := rec.get("verdict")) is not None:
        return Verdict.of(verdict_str)
    
    if (last_message := rec.get("last_message")) is None:
        return Verdict.UNKNOWN

    if last_message.startswith("RELEVANT"):
        return Verdict.RELEVANT
    elif last_message.startswith("IRRELEVANT"):
        return Verdict.IRRELEVANT
    elif last_message.startswith("UNSURE"):
        return Verdict.UNSURE

    return Verdict.UNKNOWN

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

    # Normalize verdicts and tokens.
    for rec in records:
        rec["verdict"] = compute_verdict(rec)
        if "tokens_used" not in rec:
            rec["tokens_used"] = compute_tokens_used_from_stdout(rec.get("stdout"))

    # ---------- page layout -----------------------------------------------------
    st.set_page_config(page_title="Query Results Viewer", layout="wide")
    st.title("Query Results Viewer")
    st.caption(f"Loaded **{len(records)}** record(s) from {args.input_file}.")

    # ----- filtering -----------------------------------------------------------
    with st.sidebar:
        st.subheader("Filters")
        query_filter = st.text_input(
            "Query contains",
            value="",
            help="Show only records whose SQL query contains this text (case-insensitive).",
        ).strip()

    # Apply query string filter (case-insensitive substring)
    if query_filter:
        filtered_records = [
            r for r in records if query_filter.lower() in (r.get("query", "").lower())
        ]
    else:
        filtered_records = records

    st.subheader("Summary")

    # Top-level metrics summary
    counts = collections.Counter(r["verdict"] for r in filtered_records)
    for col, verdict in zip(st.columns(4), (Verdict.RELEVANT, Verdict.IRRELEVANT, Verdict.UNSURE, Verdict.UNKNOWN)):
        col.metric(get_verdict_markdown_badge(verdict), counts.get(verdict, 0))

    # ---------- summary table ---------------------------------------------------
    summary_df = pd.DataFrame(
        {
            "Query": [preview(r["query"], 120) for r in filtered_records],
            "Verdict": [get_verdict_badge_emoji(r.get("verdict")) for r in filtered_records],
            "Duration (s)": [r.get("dur_s") for r in filtered_records],
            "Tokens used": [r.get("tokens_used") for r in filtered_records],
            "Exit code": [r.get("exit_code") for r in filtered_records],
        }
    )

    # Indicate filtering status
    if query_filter:
        st.caption(
            f"Showing {len(filtered_records)} of {len(records)} record(s) matching: '{query_filter}'"
        )

    st.dataframe(summary_df, height=min(400, 40 + 35 * len(summary_df)), use_container_width=True)

    st.divider()

    # ---------- detailed, expandable view --------------------------------------
    st.subheader("Detailed records")

    for idx, rec in enumerate(filtered_records):
        query_preview = preview(rec['query'], 120)
        with st.expander(f"**Record #{idx}:** {get_verdict_markdown_badge(rec.get('verdict'))} `` {query_preview} ``"):
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

            # Determine tabs based on whether original prompt exists
            tab_names = ["SQL", "Stack trace", "Report", "stdout", "stderr"]
            if rec.get("prompt"):
                tab_names.append("Original prompt")
            tab_names.append("Copy current prompt")
            
            tabs = st.tabs(tab_names)
            with tabs[0]:
                st.code(
                    sqlparse.format(rec["query"], reindent=True, keyword_case="upper"),
                    language="sql",
                    line_numbers=True,
                )
            with tabs[1]:
                # Render stacktrace in a scrollable, fixed-height mono box with per-frame lines
                st.code(
                    "\n".join(rec["stacktrace"]),
                    language="text",
                    line_numbers=True,
                )
            with tabs[2]:
                st.markdown(rec["last_message"])
            with tabs[3]:
                st.code(rec["stdout"])
            with tabs[4]:
                st.code(rec["stderr"])
            
            # Original prompt tab (if it exists)
            tab_idx = 5
            if rec.get("prompt"):
                with tabs[tab_idx]:
                    st.info(":material/info: This is the original prompt that was used to generate this record.")
                    st.code(rec["prompt"], language="text", line_numbers=True)
                tab_idx += 1
            
            # Copy prompt tab (always last)
            with tabs[tab_idx]:
                st.warning(
                    ":material/warning: The prompt shown below is constructed from the **current** template, "
                    "not the original prompt that was used to generate this record. "
                    "Use this to test the current template with the same query and stacktrace."
                )
                prompt = generate_prompt(rec["query"], rec["stacktrace"])
                st.code(prompt, language="text", line_numbers=True)


if __name__ == "__main__":
    main()
