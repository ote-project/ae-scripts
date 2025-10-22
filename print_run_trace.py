#!/usr/bin/env python3
"""
Print the trace for a given Run ID by scanning shard files.

Usage:
  python3 print_run_trace.py <RUN_ID> [--shards 'paths-*.txt'] [--all]

Defaults:
  --shards 'paths-*.txt'  (scan all standard shard files)

Exits with code 0 if a trace is printed, 1 if no matching Run ID is found.
"""

from __future__ import annotations

import argparse
import glob
import sys
from typing import Optional


def print_trace_for_run_in_file(path: str, run_id: str, *, print_file_header: bool = False) -> int:
    """Print the trace for the given run ID found in a single file.

    Returns the number of traces printed from this file (0 or >=1).
    """
    count = 0
    inside = False
    target = run_id
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line.startswith("Run ID: "):
                current = line.split(":", 1)[1].strip()
                if inside:
                    # End of the current trace block
                    inside = False
                    # If printing all, continue scanning; otherwise we would have returned earlier.
                if current == target:
                    # Start of desired trace
                    inside = True
                    count += 1
                    if print_file_header:
                        print(f"File: {path}")
                    print(line)
                continue
            if inside:
                print(line)
        # No special handling at EOF; if inside, we've been printing already.
    return count


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_id", help="Run ID to extract (e.g., 179142)")
    ap.add_argument(
        "--shards",
        default="paths-*.txt",
        help="Glob pattern of shard files to scan (default: paths-*.txt)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Print all occurrences across shards (default prints first and exits)",
    )
    ap.add_argument(
        "--show-file",
        action="store_true",
        help="Print a File: <path> header before each trace",
    )
    args = ap.parse_args(argv)

    files = sorted(glob.glob(args.shards))
    if not files:
        print(f"No shard files matched pattern: {args.shards}", file=sys.stderr)
        return 2

    total_printed = 0
    for path in files:
        printed = print_trace_for_run_in_file(path, args.run_id, print_file_header=args.show_file)
        total_printed += printed
        if printed and not args.all:
            break

    return 0 if total_printed else 1


if __name__ == "__main__":
    raise SystemExit(main())

