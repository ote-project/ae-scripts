#!/usr/bin/env python3
"""
Assembles run statistics from directories listed in runs.txt.
Groups runs by prefix and computes statistics.
"""
import argparse
import csv
import datetime
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Tuple


def extract_prefix(directory_name: str) -> str:
    """Extract the prefix pattern *-*-*-2r from directory name."""
    # Find the position of '-2r' in the directory name
    idx = directory_name.find('-2r')
    if idx == -1:
        return directory_name  # Fallback if pattern not found
    # Return everything up to and including '-2r'
    return directory_name[:idx + 3]


def compute_explore_dur_s(log_dir: Path) -> float:
    """
    Compute explore duration in seconds from a log directory.
    Based on make_table.py lines 205-209.
    """
    runs_csv_path = log_dir / "metrics" / "edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutionsImpl.runs.csv"
    
    if not runs_csv_path.exists():
        raise FileNotFoundError(f"Runs CSV not found: {runs_csv_path}")
    
    with open(runs_csv_path, 'r') as f:
        runs = list(csv.DictReader(f))
    
    explore_timestamps_s = [int(row["t"]) for row in runs]
    explore_dur_s = max(explore_timestamps_s) - min(explore_timestamps_s)
    
    return explore_dur_s


def main(argv=None):
    parser = argparse.ArgumentParser(description="Assemble run statistics from runs.txt.")
    parser.add_argument("--make-symlinks", action="store_true",
                        help="Create symlinks to median directories.")
    args = parser.parse_args(argv)

    # Read runs.txt
    script_dir = Path(__file__).parent
    runs_txt_path = script_dir / "runs.txt"
    
    if not runs_txt_path.exists():
        raise FileNotFoundError(f"runs.txt not found: {runs_txt_path}")
    
    with open(runs_txt_path, 'r') as f:
        directories = [line.strip() for line in f if line.strip()]
    
    # Base directory for logs
    home_dir = Path.home()
    logs_base = home_dir / "dse" / "logs"
    
    # Generate timestamp for all symlinks (only if creating them)
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") if args.make_symlinks else None
    
    # Group runs by prefix and collect durations with directory names
    groups: Dict[str, List[Tuple[float, str]]] = {}
    
    for directory_name in directories:
        prefix = extract_prefix(directory_name)
        log_dir = logs_base / directory_name
        
        if not log_dir.exists():
            print(f"Warning: Directory not found: {log_dir}", file=sys.stderr)
            continue
        
        try:
            duration = compute_explore_dur_s(log_dir)
            if prefix not in groups:
                groups[prefix] = []
            groups[prefix].append((duration, directory_name))
        except Exception as e:
            print(f"Warning: Failed to process {directory_name}: {e}", file=sys.stderr)
            continue
    
    # Compute and print statistics for each group
    largest_abs_dev: Tuple[float, str, str] | None = None  # (abs_dev, prefix, direction)
    for prefix in sorted(groups.keys()):
        runs = groups[prefix]
        count = len(runs)
        
        if count == 0:
            continue
        
        # Sort by duration to find median directory
        runs_sorted = sorted(runs, key=lambda x: x[0])
        durations = [duration for duration, _ in runs_sorted]
        
        median = statistics.median(durations)
        min_val = min(durations)
        max_val = max(durations)
        
        # Find the directory with the median value
        # For odd count, use the middle element
        # For even count, use the element at the lower middle index (or closest to median)
        if count % 2 == 1:
            median_idx = count // 2
            median_directory = runs_sorted[median_idx][1]
        else:
            # For even count, median is average of two middle values
            # Use the lower middle index
            median_idx = (count - 1) // 2
            median_directory = runs_sorted[median_idx][1]
        
        # Calculate percentage deviation from median
        min_deviation = ((min_val - median) / median * 100) if median != 0 else 0
        max_deviation = ((max_val - median) / median * 100) if median != 0 else 0

        # Track largest absolute deviation
        for dev, direction in ((min_deviation, "min"), (max_deviation, "max")):
            abs_dev = abs(dev)
            if largest_abs_dev is None or abs_dev > largest_abs_dev[0]:
                largest_abs_dev = (abs_dev, prefix, direction)
        
        # Create symlink to median directory if requested
        if args.make_symlinks:
            symlink_name = f"{prefix}-osdi26-{timestamp}"
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

    # After all groups, report the largest absolute deviation
    if largest_abs_dev is not None:
        abs_dev, prefix, direction = largest_abs_dev
        print(f"Largest absolute deviation: {abs_dev:.2f}% ({direction} of group {prefix})")
    else:
        print("Largest absolute deviation: N/A (no valid runs processed)")


if __name__ == "__main__":
    main()
