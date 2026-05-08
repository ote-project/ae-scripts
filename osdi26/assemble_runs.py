#!/usr/bin/env python3
"""
Assembles run statistics from a list of run directory names.
Groups runs by handler prefix (`<app>-<controller>-<action>-2r`), reports
per-group median exploration time, and optionally creates symlinks pointing
to the median directory of each group.
"""
import argparse
import csv
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Tuple


def extract_prefix(directory_name: str) -> str:
    """Extract the prefix pattern *-*-*-2r from directory name."""
    idx = directory_name.find('-2r')
    if idx == -1:
        return directory_name  # Fallback if pattern not found
    return directory_name[:idx + 3]


def compute_explore_dur_s(log_dir: Path) -> float:
    """
    Compute explore duration in seconds from a log directory.
    Reads metrics/...runs.csv and returns max(t) - min(t).
    """
    runs_csv_path = log_dir / "metrics" / "edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutionsImpl.runs.csv"

    if not runs_csv_path.exists():
        raise FileNotFoundError(f"Runs CSV not found: {runs_csv_path}")

    with open(runs_csv_path, 'r') as f:
        runs = list(csv.DictReader(f))

    explore_timestamps_s = [int(row["t"]) for row in runs]
    return max(explore_timestamps_s) - min(explore_timestamps_s)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Assemble run statistics; optionally create median symlinks."
    )
    parser.add_argument(
        "run_dirs", nargs="*", default=[],
        help="Run directory names (relative to ~/dse/logs/).",
    )
    parser.add_argument(
        "--runs-file", type=Path, default=None,
        help="Path to a file with run directory names, one per line.",
    )
    parser.add_argument(
        "--make-symlinks", action="store_true",
        help="Create symlinks pointing to the median directory of each group.",
    )
    parser.add_argument(
        "--out-suffix", type=str, default=None,
        help="Symlink suffix: <prefix>-<out-suffix>. Required with --make-symlinks.",
    )
    args = parser.parse_args(argv)

    if args.make_symlinks and args.out_suffix is None:
        parser.error("--make-symlinks requires --out-suffix")

    directories: List[str] = list(args.run_dirs)
    if args.runs_file is not None:
        if not args.runs_file.exists():
            parser.error(f"--runs-file not found: {args.runs_file}")
        with open(args.runs_file, 'r') as f:
            directories.extend(line.strip() for line in f if line.strip())

    if not directories:
        parser.error("No run directories provided. Pass them as positional args or via --runs-file.")

    logs_base = Path.home() / "dse" / "logs"

    # Group runs by prefix and collect (duration, directory_name) pairs.
    groups: Dict[str, List[Tuple[float, str]]] = {}

    for directory_name in directories:
        prefix = extract_prefix(directory_name)
        log_dir = logs_base / directory_name

        if not log_dir.exists():
            print(f"Warning: Directory not found: {log_dir}", file=sys.stderr)
            continue

        try:
            duration = compute_explore_dur_s(log_dir)
            groups.setdefault(prefix, []).append((duration, directory_name))
        except Exception as e:
            print(f"Warning: Failed to process {directory_name}: {e}", file=sys.stderr)
            continue

    largest_abs_dev: Tuple[float, str, str] | None = None  # (abs_dev, prefix, direction)
    for prefix in sorted(groups.keys()):
        runs = groups[prefix]
        count = len(runs)
        if count == 0:
            continue

        runs_sorted = sorted(runs, key=lambda x: x[0])
        durations = [d for d, _ in runs_sorted]
        median = statistics.median(durations)
        min_val = min(durations)
        max_val = max(durations)

        # For odd count: middle element. For even count: lower middle.
        median_idx = count // 2 if count % 2 == 1 else (count - 1) // 2
        median_directory = runs_sorted[median_idx][1]

        min_deviation = ((min_val - median) / median * 100) if median != 0 else 0
        max_deviation = ((max_val - median) / median * 100) if median != 0 else 0

        for dev, direction in ((min_deviation, "min"), (max_deviation, "max")):
            abs_dev = abs(dev)
            if largest_abs_dev is None or abs_dev > largest_abs_dev[0]:
                largest_abs_dev = (abs_dev, prefix, direction)

        if args.make_symlinks:
            symlink_name = f"{prefix}-{args.out_suffix}"
            symlink_path = logs_base / symlink_name
            target_path = logs_base / median_directory

            if symlink_path.exists():
                raise FileExistsError(f"Symlink already exists: {symlink_path}")
            symlink_path.symlink_to(target_path)

        print(f"{prefix}:")
        print(f"  Count: {count}")
        print(f"  Median: {median:.2f}s ({median_directory})")
        print(f"  Min deviation from median: {min_deviation:.2f}%")
        print(f"  Max deviation from median: {max_deviation:.2f}%")
        print()

    if largest_abs_dev is not None:
        abs_dev, prefix, direction = largest_abs_dev
        print(f"Largest absolute deviation: {abs_dev:.2f}% ({direction} of group {prefix})")
    else:
        print("Largest absolute deviation: N/A (no valid runs processed)")


if __name__ == "__main__":
    main()
