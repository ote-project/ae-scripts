from __future__ import annotations

import concurrent.futures
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import List, Optional
import re

import duckdb


# Accept both gzip and zstd compressed JSON inputs
# Example filenames: paths-*.json.gz, paths-*.json.zst
INPUT_FILE_GLOB_PATTERNS = [
    "paths-*.json.zst",
    "paths-*.json.gz",
]

EVENT_SHARDS_DIRNAME = "event_shards"


def _write_events_shard(json_file: str, out_dir: str, memory_limit: Optional[str] = None) -> str:
    """Worker: read one JSON(.gz|.zst) file, transform to event rows, and write a Parquet shard.
    Returns the output shard path.
    """
    # Deterministic shard name from source file
    base = os.path.basename(json_file)
    name, _ = os.path.splitext(base)  # .gz/.zst or .json.gz/.json.zst -> leave trailing compressed ext trimmed
    if name.endswith(".json"):
        name = name[:-5]
    shard_path = os.path.join(out_dir, f"{name}.parquet")

    with tempfile.TemporaryDirectory() as tmpdir:
        con = duckdb.connect()
        # Constrain each worker to a single DuckDB thread to avoid per-worker thread blow-up
        con.execute("PRAGMA threads=1;")
        con.execute("PRAGMA temp_directory=?", [tmpdir])
        con.execute("PRAGMA preserve_insertion_order=false;")
        # Optionally cap each worker's memory budget
        if memory_limit and str(memory_limit).strip().lower() != 'system':
            con.execute("PRAGMA memory_limit=?", [str(memory_limit)])
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
        return name[:-5] if name.endswith(".json") else name

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
            listed = ", ".join(sorted(str(x.name) for x in paths))
            parts.append(f"{k}: [{listed}]")
        details = "; ".join(parts)
        raise ValueError(f"duplicate inputs for base name(s): {details}")

    files = [p for lst in by_key.values() for p in lst]
    return [str(p) for p in sorted(files, key=lambda x: x.name)]


def _parse_memory_bytes(s: str) -> Optional[int]:
    """Parse DuckDB-style memory strings like '8GB', '1024MB', return bytes.

    Returns None for 'system' or empty values.
    """
    if s is None:
        return None
    t = str(s).strip().lower()
    if t == '' or t == 'system':
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([kmgt]?b?)$", t)
    if not m:
        # Fallback: try to parse as integer bytes
        try:
            return int(float(t))
        except Exception:
            return None
    val = float(m.group(1))
    unit = m.group(2)
    mult = 1
    if unit in ('k', 'kb'):
        mult = 1024
    elif unit in ('m', 'mb'):
        mult = 1024 ** 2
    elif unit in ('g', 'gb'):
        mult = 1024 ** 3
    elif unit in ('t', 'tb'):
        mult = 1024 ** 4
    return int(val * mult)


def _format_duckdb_mem(nbytes: int) -> str:
    """Format a byte value for DuckDB PRAGMA memory_limit (prefer MB granularity)."""
    # Use MB to avoid decimals and keep things explicit
    mb = max(1, int(nbytes // (1024 ** 2)))
    return f"{mb}MB"


def _per_worker_memory(total: str, workers: int) -> Optional[str]:
    """Compute per-worker memory cap string from a total budget.

    If total is 'system' or unparsable, returns None (no cap). Ensures a
    reasonable floor to avoid pathological low values.
    """
    if not total:
        return None
    b = _parse_memory_bytes(total)
    if b is None:
        return None
    w = max(1, int(workers or 1))
    per = b // w
    # Apply a conservative floor (128MB) to keep DuckDB operational on tiny budgets
    floor = 128 * 1024 * 1024
    per = max(per, floor)
    return _format_duckdb_mem(per)


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
            progress_cb(0, total, "")

        # Prepare temp shards directory under annotated-paths/
        final_shards_dir = data_dir / EVENT_SHARDS_DIRNAME
        # Build shards inside the function-scoped temporary directory first
        sys_tmp_shards_dir = tmpdir_path / f".{EVENT_SHARDS_DIRNAME}.build-{uuid.uuid4().hex}"
        sys_tmp_shards_dir.mkdir(parents=True, exist_ok=True)

        # Fan out per-file workers
        done = 0
        per_worker_mem = _per_worker_memory(memory_limit, threads)
        with concurrent.futures.ProcessPoolExecutor(max_workers=threads) as pool:
            futs = [
                pool.submit(_write_events_shard, f, str(sys_tmp_shards_dir), per_worker_mem)
                for f in files
            ]
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
            progress_cb(total, total, "reducing: building views…")

        tmp_index_path = tmpdir_path / "ap_index.duckdb"
        con = duckdb.connect(str(tmp_index_path))
        con.execute("PRAGMA threads=?", [threads])
        con.execute("PRAGMA preserve_insertion_order=?", [False])
        con.execute("PRAGMA memory_limit=?", [memory_limit])
        con.execute("PRAGMA temp_directory=?", [tmpdir])

        shards_glob = str((final_shards_dir / "*.parquet").as_posix())
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

        con.close()  # Ensure all data is flushed and connection is closed before replace
        tmp_index_path.replace(index_path)  # Atomically move into place (overwriting any existing index)
