from pathlib import Path
from typing import List

import duckdb
import pandas as pd


class APDB:
    """Tiny helper for DuckDB connection and common queries."""

    @staticmethod
    def connect(index_path: Path, *, threads: int, memory_limit: str) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(str(index_path))
        con.execute("PRAGMA threads=?", [int(threads)])
        con.execute("PRAGMA preserve_insertion_order=?", [False])
        con.execute("PRAGMA memory_limit=?", [str(memory_limit)])
        return con

    @staticmethod
    def get_index_stats(con) -> tuple[int, int]:
        n_traces = int(con.execute("SELECT count(*) FROM traces").fetchone()[0])
        n_events_row = con.execute("SELECT coalesce(sum(n_events),0) FROM traces").fetchone()
        n_events = int(n_events_row[0]) if n_events_row and n_events_row[0] is not None else 0
        return n_traces, n_events

    @staticmethod
    def fetch_traces(con, *, sql_sub: str = '', min_sql: int = 0, min_conds: int = 0, limit: int = 10) -> pd.DataFrame:
        where = ["1=1"]
        params: List[object] = []
        if sql_sub:
            where.append("EXISTS (SELECT 1 FROM queries q WHERE q.runId=t.runId AND q.query_lc LIKE ?)")
            params.append(f"%{sql_sub.lower()}%")
        if min_sql:
            where.append("n_sql >= ?")
            params.append(int(min_sql))
        if min_conds:
            where.append("n_conds >= ?")
            params.append(int(min_conds))

        base_sql = f"""
          SELECT runId, file, n_events, n_sql, n_conds
          FROM traces t
          WHERE {' AND '.join(where)}
          ORDER BY n_events DESC
          LIMIT {int(limit)}
        """
        return con.execute(base_sql, params).fetchdf()

    @staticmethod
    def fetch_summary(con, run_id: int) -> pd.DataFrame:
        return con.execute(
            "SELECT runId, file, n_events, n_sql, n_conds FROM traces WHERE runId=?",
            [int(run_id)],
        ).fetchdf()

    @staticmethod
    def fetch_events(con, run_id: int) -> pd.DataFrame:
        return con.execute(
            "SELECT * FROM events WHERE runId = ? ORDER BY event_idx",
            [int(run_id)],
        ).fetchdf()

