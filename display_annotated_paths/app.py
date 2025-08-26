from collections import Counter
import json
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import List, Optional

import pandas as pd
import sqlparse
import streamlit as st

# Optional deps are imported lazily so the app can render helpful
# guidance instead of crashing when packages are missing.
try:
    import duckdb  # type: ignore
except Exception:  # pragma: no cover
    duckdb = None  # type: ignore


st.set_page_config(page_title="Annotated Paths Browser", layout="wide")


@st.cache_resource(show_spinner=False)
def get_duckdb_conn(db_path: Optional[str] = None):
    if duckdb is None:
        raise RuntimeError(
            "duckdb is not installed. In your venv, run: pip install duckdb"
        )
    # Use in-memory by default; a file path can be provided to persist views.
    con = duckdb.connect(database=db_path or ":memory:")
    # Conservative defaults to avoid OOMs; can be overridden via Advanced settings.
    con.execute("PRAGMA threads=4")
    con.execute("PRAGMA preserve_insertion_order=false")
    con.execute("PRAGMA memory_limit='8GB'")
    # Ensure a temp directory exists for spill-to-disk operations.
    tmpdir = Path("display_annotated_paths/.duckdb_tmp")
    try:
        tmpdir.mkdir(parents=True, exist_ok=True)
        tmp_sql = str(tmpdir).replace("'", "''")
        con.execute(f"PRAGMA temp_directory='{tmp_sql}'")
    except Exception:
        pass
    return con


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
    progress_cb: Optional[callable] = None,
) -> None:
    """Build a persistent DuckDB index (.duckdb) over the entire dataset.

    Creates tables: events, traces, queries, conds.
    """
    if duckdb is None:
        raise RuntimeError("duckdb is not installed. Run: pip install duckdb")

    base = Path(data_dir).expanduser()
    files = sorted(str(p) for p in base.glob("paths-with-conds-*.json.gz"))
    if not files:
        raise RuntimeError("No input files found to index.")

    con = duckdb.connect(index_path)
    con.execute(f"PRAGMA threads={int(threads)}")
    con.execute("PRAGMA preserve_insertion_order=false")
    con.execute(f"PRAGMA memory_limit={_sql_quote(memory_limit)}")

    # Helper to produce the SELECT for one file
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

    # Create events from the first file to establish schema
    first = files[0]
    first_lit = _sql_quote(first)
    con.execute(f"CREATE OR REPLACE TABLE events AS {events_select_sql(first_lit)};")
    if progress_cb:
        progress_cb(1, total)

    # Append remaining files
    for i, f in enumerate(files[1:], start=1):
        flit = _sql_quote(f)
        con.execute(f"INSERT INTO events {events_select_sql(flit)};")
        if progress_cb:
            progress_cb(i + 1, total)

    # traces table
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

    # optional helper tables for faster filters
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


@dataclass(frozen=True)
class DuckDBConfig:
    threads: int
    memory_limit: str
    preserve_insertion_order: bool


@dataclass(frozen=True)
class AppInputs:
    data_dir: str
    index_path: str
    build_index: bool
    sql_sub: str
    min_sql: int
    min_conds: int
    show_details: bool
    duckdb: DuckDBConfig


def _render_sidebar(default_data_dir: Optional[str]) -> AppInputs:
    st.sidebar.header("Filters")

    data_dir = st.sidebar.text_input(
        "Data directory",
        value=default_data_dir or "",
        help="Directory containing paths-with-conds-*.json.gz",
    )

    files: List[str] = []
    if data_dir:
        try:
            files = _list_input_files(data_dir)
        except Exception:
            files = []
    st.sidebar.caption(f"Found {len(files)} files")

    # Index controls
    st.sidebar.subheader("Index")
    # Default index lives alongside the data, for clarity and portability
    if data_dir:
        default_index = str(Path(data_dir) / "ap_index.duckdb")
    else:
        default_index = ""
    index_path = st.sidebar.text_input("Index path (.duckdb)", value=default_index)
    build_clicked = st.sidebar.button("Build/Refresh Full Index", help="Parse all JSON files into a DuckDB index for fast queries")

    # Always use index; no raw scan selection

    sql_sub = st.sidebar.text_input("SQL contains", value="")
    min_sql = st.sidebar.number_input("Min #SQL", min_value=0, step=1, value=0)
    min_conds = st.sidebar.number_input("Min #Conds", min_value=0, step=1, value=0)

    # Advanced DuckDB settings
    show_details = st.sidebar.checkbox("Show query details", value=False, help="Show expanded SQL and stacktraces")

    with st.sidebar.expander("Advanced (DuckDB)"):
        threads = st.number_input("threads", min_value=1, max_value=64, value=4, step=1,
                                  help="Lower to reduce memory spikes.")
        mem_limit = st.text_input("memory_limit", value="8GB",
                                  help="DuckDB memory limit, e.g., 2GB, 8GB")
        preserve_order = st.checkbox("preserve_insertion_order", value=False)

    return AppInputs(
        data_dir=data_dir.strip(),
        index_path=index_path.strip(),
        build_index=bool(build_clicked),
        sql_sub=sql_sub.strip(),
        min_sql=int(min_sql),
        min_conds=int(min_conds),
        show_details=bool(show_details),
        duckdb=DuckDBConfig(
            threads=int(threads),
            memory_limit=mem_limit.strip() or "8GB",
            preserve_insertion_order=bool(preserve_order),
        ),
    )


def _vac_badge_md(v) -> str:
    match v:
        case "Vacuous":
            return ':red-badge[Vacuous]'
        case "NonVacuous":
            return ':green-badge[NonVacuous]'
        case _:
            raise ValueError(f"Unknown vacuousness: {v}")


def _path_condition_atom_badge_md(outcome) -> str:
    if bool(outcome):
        return ':blue-badge[:material/south_west: T]'
    else:
        return ':blue-badge[:material/south_east: F]'


def _query_badge_md(qi: int) -> str:
    return f':violet-badge[:material/database_search: Q{qi}]'


def _qidx_val(v) -> int:
    return int(v["value"])


def _escape_underscores(s: str) -> str:
    return s.replace("_", "\\_")


def _fmt_qrv(term: dict) -> str:
    q = term["qIdx"]["value"]
    r = term["rowIdx"]["value"]
    c = term["colIdx"]["value"]
    # rep = f"Q{q}_R{r}_C{c}"
    rep = f"Q_{{{q}}}R_{{{r}}}C_{{{c}}}"

    if (name := term.get("colName")) is not None:
        # rep = f"{rep}:{name}"
        name = _escape_underscores(name)
        rep = f"{rep}[\\texttt{{{name}}}]"

    # return f"`{rep}`"
    return rep

def _fmt_term(term) -> str:
    return f"${_fmt_term_inner(term)}$"


def _fmt_term_inner(term) -> str:
    # Render literals/params/cells in programmer-friendly pseudocode
    if term == "ConstTrue":
        return r"\top"

    if term == "ConstFalse":
        return r"\bot"

    if not isinstance(term, dict):
        raise ValueError(f"Unknown term structure: {term}")

    match term["$type"]:
        case "ConstString":
            v = term["value"]
            return r'\texttt{"' + _escape_underscores(v) + '"}'
        case "ConstLong":
            return str(term["value"])
        case "QueryResVar":
            return _fmt_qrv(term)
        case "DeclaredVar":
            return r'\texttt{' + _escape_underscores(term["name"]) + '}'
        case "UnaryOp":
            op = r'\text{' + _escape_underscores(term["op"]) + '}'
            operand = _fmt_term_inner(term["operand"])
            return f"{op}({operand})"
        case "BinaryOp":
            # FIXME(zhangwen): may need to parenthesize.
            lhs = _fmt_term_inner(term["lhs"])
            rhs = _fmt_term_inner(term["rhs"])
            if term["op"] == "Eq":
                return f"{lhs} = {rhs}"
            else:
                op = r'\text{' + _escape_underscores(term["op"]) + '}'
                return f"{op}({lhs}, {rhs})"
        case _:
            raise ValueError(f"Unknown term: {term}")


def _display_sql_query_decl(r, show_details=False):
    qi = _qidx_val(r["qIdx"])
    query = r["query"]
    st.markdown(f"[{int(r['event_idx'])}] {_query_badge_md(qi)} ``{query}``")

    _display_params(r["params"])

    if show_details:
        with st.expander("Query", expanded=False):
            st.code(sqlparse.format(query, reindent=True, keyword_case='upper'), language="sql")

        with st.expander("Stacktrace", expanded=False):
            st.code(r["stacktrace"], language="text")


def _display_sql_query_res_row_decl(r, row_counters: Counter[int]) -> None:
    qi = r["qIdx"]["value"]
    row_id = row_counters[qi]
    row_counters[qi] += 1
    st.markdown(f"""
        &nbsp;&nbsp;&nbsp;&nbsp;[{int(r['event_idx'])}]
        :violet-badge[:material/add_row_below:]
        {_vac_badge_md(r['vacuousness'])} $Q_{{{qi}}}R_{{{row_id}}}$
    """)


def _display_sql_query_res_end(r):
    st.markdown(f"""
        &nbsp;&nbsp;&nbsp;&nbsp;[{int(r['event_idx'])}]
        :violet-badge[:material/line_end:]
        {_vac_badge_md(r['vacuousness'])}
        (End)
    """)


def _display_path_condition_atom(r):
    cond = r["cond"]
    badge = _path_condition_atom_badge_md(r["outcome"])
    pseudo = _fmt_term(cond)
    st.markdown(f"[{int(r['event_idx'])}] {badge} {_vac_badge_md(r['vacuousness'])} {pseudo}")


def _json_default(x):  # lenient fallback for non-JSON-serializable values
    try:
        # Convert common numeric wrappers
        import numpy as np  # type: ignore
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


def _render_jsonlike(obj) -> None:
    import json as _json
    if obj is None:
        st.write("(none)")
        return
    if isinstance(obj, (dict, list)):
        try:
            st.json(obj)
            return
        except Exception:
            pass
    if isinstance(obj, str):
        try:
            st.json(_json.loads(obj))
            return
        except Exception:
            st.code(obj, language="json")
            return
    # Fallback: pretty-print best-effort JSON string
    try:
        st.code(_json.dumps(obj, default=_json_default, indent=2), language="json")
    except Exception:
        st.text(str(obj))


def _display_params(params):
    if params.size == 0:
        return

    # st.code("\n".join(
    #     f"${i} = {_fmt_value(json.loads(item))}" for i, item in enumerate(params, start=1)
    # ))
    st.table({
        "Param": [_fmt_term(json.loads(item)) for item in params]
    })


def _make_element_container():
    return st.container(border=True)


def main():
    st.title("Annotated Paths Browser")
    st.caption("Filter, browse, and drill into gzipped NDJSON program traces.")

    default_data_dir = sys.argv[1] if len(sys.argv) > 1 else None
    inputs = _render_sidebar(default_data_dir)
    if not inputs.data_dir:
        st.info("Provide a data directory to begin.")
        return

    # Prepare views / source
    try:
        cfg = inputs.duckdb
        # Build index on demand
        if inputs.build_index:
            files = _list_input_files(inputs.data_dir)
            if not files:
                st.warning("No files found to index.")
                return
            prog = st.progress(0, text=f"0/{len(files)} files")
            def _cb(done: int, total: int):
                pct = int(done / total * 100) if total else 100
                prog.progress(pct, text=f"{done}/{total} files")
            build_full_index(
                data_dir=inputs.data_dir,
                index_path=inputs.index_path,
                threads=int(cfg.threads),
                memory_limit=str(cfg.memory_limit) or "system",
                progress_cb=_cb,
            )
            prog.progress(100, text=f"{len(files)}/{len(files)} files — complete")

        # Always use index
        idx_path = Path(inputs.index_path).expanduser()
        if not idx_path.exists():
            st.warning("Index not found. Click 'Build/Refresh Full Index' to create it.")
            return
        con = duckdb.connect(str(idx_path))
        con.execute(f"PRAGMA threads={int(cfg.threads)}")
        con.execute(f"PRAGMA preserve_insertion_order={'true' if cfg.preserve_insertion_order else 'false'}")
        ml = str(cfg.memory_limit).replace("'", "''")
        con.execute(f"PRAGMA memory_limit='{ml}'")
        # Ensure required tables exist
        try:
            have_traces = con.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_name='traces'"
            ).fetchone()[0] > 0
        except Exception:
            have_traces = False
        if not have_traces:
            st.warning("Index exists but is missing required tables. Click 'Build/Refresh Full Index'.")
            return
    except Exception as e:  # pragma: no cover
        if duckdb is None:
            st.error("duckdb not installed. Run: pip install duckdb")
        st.exception(e)
        return

    # Index health/status
    with st.sidebar.expander("Index status", expanded=False):
        try:
            n_traces = con.execute("SELECT count(*) FROM traces").fetchone()[0]
            # Derive totals from traces to avoid scanning events
            row = con.execute("SELECT coalesce(sum(n_events),0), coalesce(sum(n_sql),0), coalesce(sum(n_conds),0) FROM traces").fetchone()
            n_events, n_sql, n_conds = row[0], row[1], row[2]
        except Exception:
            n_traces = n_events = n_sql = n_conds = 0
        st.metric("traces", n_traces)
        st.metric("events", n_events)
        st.metric("sql queries", n_sql)
        st.metric("conditions", n_conds)
        ip = Path(inputs.index_path).expanduser()
        try:
            size_mb = ip.stat().st_size / (1024 * 1024)
            st.caption(f"Index: {ip} ({size_mb:.1f} MB)")
        except Exception:
            st.caption(f"Index: {ip}")

    # Build dynamic predicates
    where = ["1=1"]
    params: List[object] = []

    if inputs.sql_sub:
        where.append(
            "EXISTS (SELECT 1 FROM events e WHERE e.runId=t.runId AND e.type='SqlQueryDecl' AND lower(e.query) LIKE ?)"
        )
        params.append(f"%{inputs.sql_sub.lower()}%")

    if inputs.min_sql:
        where.append("n_sql >= ?")
        params.append(inputs.min_sql)

    if inputs.min_conds:
        where.append("n_conds >= ?")
        params.append(inputs.min_conds)

    base_sql = f"""
      SELECT runId, file, n_events, n_sql, n_conds
      FROM traces t
      WHERE {' AND '.join(where)}
      ORDER BY n_events DESC
      LIMIT 10
    """

    st.subheader("Traces")
    try:
        df = con.execute(base_sql, params).fetchdf()
    except Exception as e:  # pragma: no cover
        st.exception(e)
        return

    st.dataframe(df, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("Run Detail")

    # Get default runId (first one in dataset)
    default_run_id = int(df["runId"].iloc[0]) if not df.empty else 0
    run_id = st.number_input("runId", min_value=0, step=1, value=default_run_id, key="runid_input")

    if run_id is not None:
        summary = con.execute(
            "SELECT runId, file, n_events, n_sql, n_conds FROM traces WHERE runId=?",
            [run_id],
        ).fetchdf()
        ev_df = con.execute(
            "SELECT event_idx, type, qIdx, query, params, stacktrace, vacuousness, cond, outcome "
            "FROM events WHERE runId = ? ORDER BY event_idx",
            [run_id],
        ).fetchdf()

        # Header
        if not summary.empty:
            s = summary.iloc[0]
            st.subheader(f":material/table: Run {run_id}")
            st.caption(f"file: {s['file']}")
            m1, m2, m3 = st.columns(3)
            m1.metric("events", int(s["n_events"]))
            m2.metric("sql queries", int(s["n_sql"]))
            m3.metric("conditions", int(s["n_conds"]))

        tabs = st.tabs(["Timeline", "Raw"])

        with tabs[0]:
            # Row counters per qIdx within this run (start at 0 for each query)
            row_counters: Counter[int] = Counter()
            current_container = None
            
            for _, r in ev_df.iterrows():
                match r["type"]:
                    case "SqlQueryDecl":
                        # Start a new container for this query and its results
                        with (current_container := _make_element_container()):
                            _display_sql_query_decl(r, inputs.show_details)
                    case "SqlQueryResRowDecl":
                        # Add to current query container if it exists
                        if current_container is None:
                            raise ValueError("SqlQueryResRowDecl without preceding SqlQueryDecl")

                        with current_container:
                            _display_sql_query_res_row_decl(r, row_counters)
                    case "SqlQueryResEnd":
                        # Add to current query container if it exists, then close it
                        if current_container is None:
                            raise ValueError("SqlQueryResEnd without preceding SqlQueryDecl")

                        with current_container:
                            _display_sql_query_res_end(r)
                        current_container = None  # Close the container
                    case "PathConditionAtom":
                        # Path conditions are standalone, display outside containers
                        with _make_element_container():
                            _display_path_condition_atom(r)
                    case _:
                        raise ValueError(f"Unknown event type: {r['type']}")

        with tabs[1]:
            st.write("Events (raw)")
            st.dataframe(ev_df, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
