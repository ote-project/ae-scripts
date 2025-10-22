#!/usr/bin/env python3
"""
Find runs that contain a fragment (set of lines) as a subset modulo query-number
renaming (Q-ids), across concatenated trace files separated by 'Run ID: <n>'.

Usage:
  python3 match_modulo_q.py --fragment fragment.txt [--shards 'paths-*.txt'] [--show-mapping] [--workers N]
  Prints all matching runs, not just the first.

Notes:
  - Assumes the fragment has no duplicate lines. If duplicates exist, an error
    is raised unless --allow-duplicate-fragment-lines is passed.
  - Matching is order-agnostic (subset, not subsequence) and allows a consistent
    injective renaming from fragment Q-ids to run Q-ids.
  - Only Q-number tokens (e.g., Q0, Q12) may be renamed; the rest of the text,
    including row/column tokens like R0/C3 and all SQL, must match exactly.
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
from collections import defaultdict
import concurrent.futures as _fut
import os
from dataclasses import dataclass
from typing import Dict, List, Tuple, Iterable, Optional


QRE = re.compile(r"Q\d+")


@dataclass
class FragItem:
    line: str
    sk: str
    qs: List[str]
    eq_groups: List[List[int]]  # positions in qs that must be equal within a line


@dataclass
class CandItem:
    line: str
    qs: List[str]
    used: bool = False


def skeleton_and_qs(line: str) -> Tuple[str, List[str]]:
    qs = QRE.findall(line)
    sk = QRE.sub("Q@", line)
    return sk, qs


def parse_fragment(path: str, allow_duplicate_lines: bool = False) -> List[FragItem]:
    with open(path, "r", encoding="utf-8") as f:
        # Strip trailing newlines; drop empty/whitespace-only lines from fragment
        frag_lines = [ln.rstrip("\n") for ln in f]
        frag_lines = [ln for ln in frag_lines if ln.strip() != ""]

    if not allow_duplicate_lines:
        dups = len(frag_lines) - len(set(frag_lines))
        if dups:
            raise ValueError(
                f"Fragment contains {dups} duplicate line(s). Either deduplicate or pass --allow-duplicate-fragment-lines."
            )

    items: List[FragItem] = []
    for line in frag_lines:
        sk, qs = skeleton_and_qs(line)
        pos_by_q: Dict[str, List[int]] = defaultdict(list)
        for i, q in enumerate(qs):
            pos_by_q[q].append(i)
        eq_groups = [poses for poses in pos_by_q.values() if len(poses) > 1]
        items.append(FragItem(line=line, sk=sk, qs=qs, eq_groups=eq_groups))
    return items


def run_matches(
    run_lines: Iterable[str],
    frag_items: List[FragItem],
    return_mapping: bool = False,
) -> Tuple[bool, Optional[Dict[str, str]]]:
    """Backtracking matcher: does the run contain the fragment modulo Q renaming?

    Returns (matched: bool, mapping or None)
    """
    # Build index by skeleton
    sk_to_candidates: Dict[str, List[CandItem]] = defaultdict(list)
    run_sks = set()
    for line in run_lines:
        sk, qs = skeleton_and_qs(line)
        sk_to_candidates[sk].append(CandItem(line=line, qs=qs))
        run_sks.add(sk)

    # Quick precheck: every fragment skeleton must exist in run
    frag_sks = {fi.sk for fi in frag_items}
    if not frag_sks.issubset(run_sks):
        return False, None

    # Order frag items by increasing candidate count (most selective first)
    order = sorted(range(len(frag_items)), key=lambda i: len(sk_to_candidates[frag_items[i].sk]))

    phi: Dict[str, str] = {}
    used_run_q: set[str] = set()  # enforce injectivity

    def backtrack(k: int) -> bool:
        if k == len(order):
            return True
        fi = frag_items[order[k]]
        candidates = sk_to_candidates[fi.sk]
        for cand in candidates:
            if cand.used:
                continue
            rq = cand.qs
            fq = fi.qs
            if len(rq) != len(fq):
                continue
            # Within-line equality: positions with same frag Q must match same run Q
            ok = True
            for poses in fi.eq_groups:
                first = rq[poses[0]]
                if any(rq[p] != first for p in poses[1:]):
                    ok = False
                    break
            if not ok:
                continue
            # Check/extend mapping
            updates: List[Tuple[str, str]] = []
            for j in range(len(fq)):
                fQ = fq[j]
                rQ = rq[j]
                if fQ in phi:
                    if phi[fQ] != rQ:
                        ok = False
                        break
                else:
                    if rQ in used_run_q:
                        ok = False
                        break
                    updates.append((fQ, rQ))
            if not ok:
                continue
            # Commit and recurse
            for fQ, rQ in updates:
                phi[fQ] = rQ
                used_run_q.add(rQ)
            cand.used = True
            if backtrack(k + 1):
                return True
            cand.used = False
            for fQ, rQ in updates:
                used_run_q.remove(rQ)
                del phi[fQ]
        return False

    matched = backtrack(0)
    if not matched:
        return False, None
    return True, (phi.copy() if return_mapping else None)


def iter_runs_in_file(path: str) -> Iterable[Tuple[str, List[str]]]:
    """Yield (run_id, lines) for each run in the file."""
    current_id: Optional[str] = None
    buf: List[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line.startswith("Run ID: "):
                if current_id is not None:
                    yield current_id, buf
                current_id = line.split(":", 1)[1].strip()
                buf = []
            else:
                buf.append(line)
        if current_id is not None:
            yield current_id, buf


def _scan_file_for_match(
    path: str,
    frag_items: List[FragItem],
    return_mapping: bool,
) -> List[Tuple[str, str, Optional[Dict[str, str]]]]:
    """Worker helper: return all matches found in file.

    Returns list of (path, run_id, mapping_or_none) for each match in run order.
    """
    results: List[Tuple[str, str, Optional[Dict[str, str]]]] = []
    for run_id, run_lines in iter_runs_in_file(path):
        ok, mapping = run_matches(run_lines, frag_items, return_mapping=return_mapping)
        if ok:
            results.append((path, run_id, (mapping if return_mapping else None)))
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fragment", required=True, help="Path to fragment file")
    ap.add_argument(
        "--shards",
        default="paths-*.txt",
        help="Glob pattern for shard files (default: paths-*.txt)",
    )
    ap.add_argument(
        "--show-mapping",
        action="store_true",
        help="Include the discovered Q-id renaming in output",
    )
    ap.add_argument(
        "--allow-duplicate-fragment-lines",
        action="store_true",
        help="Permit duplicate lines in fragment (treat as set)",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="Number of parallel worker processes (default: CPU count)",
    )
    args = ap.parse_args(argv)

    try:
        frag_items = parse_fragment(
            args.fragment, allow_duplicate_lines=args.allow_duplicate_fragment_lines
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    matched = 0
    files = sorted(glob.glob(args.shards))
    if not files:
        print(f"No shard files match pattern: {args.shards}", file=sys.stderr)
        return 2

    # Parallel path: scan shards concurrently and print all matches.
    if (args.workers or 1) > 1 and len(files) > 1:
        results_by_path: Dict[str, List[Tuple[str, str, Optional[Dict[str, str]]]]] = {}
        executor = _fut.ProcessPoolExecutor(max_workers=args.workers)
        try:
            future_by_path = {
                path: executor.submit(
                    _scan_file_for_match, path, frag_items, args.show_mapping
                )
                for path in files
            }
            for path, fut in future_by_path.items():
                try:
                    results_by_path[path] = fut.result()
                except Exception as e:
                    print(f"ERROR in worker for {path}: {e}", file=sys.stderr)
                    results_by_path[path] = []
        finally:
            try:
                executor.shutdown(wait=True)
            except Exception:
                pass

        for path in files:
            for _path, run_id, mapping in results_by_path.get(path, []):
                matched += 1
                if args.show_mapping and mapping:
                    mapping_str = ", ".join(
                        f"{k}->{v}" for k, v in sorted(mapping.items())
                    )
                    print(f"{path}\t{run_id}\t{mapping_str}")
                else:
                    print(f"{path}\t{run_id}")
    else:
        # Sequential: print all matches while preserving ordering.
        for path in files:
            for run_id, run_lines in iter_runs_in_file(path):
                ok, mapping = run_matches(
                    run_lines, frag_items, return_mapping=args.show_mapping
                )
                if ok:
                    matched += 1
                    if args.show_mapping and mapping:
                        mapping_str = ", ".join(
                            f"{k}->{v}" for k, v in sorted(mapping.items())
                        )
                        print(f"{path}\t{run_id}\t{mapping_str}")
                    else:
                        print(f"{path}\t{run_id}")

    return 0 if matched > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
