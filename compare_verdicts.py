#!/usr/bin/env python3
"""
Compare key_digest -> verdict mappings between two directories of JSONL files.
"""
import json
import sys
from pathlib import Path
from collections import defaultdict


def load_mappings(directory):
    """Load all key_digest -> record mappings from JSONL files in a directory."""
    mappings = {}
    directory_path = Path(directory)

    if not directory_path.exists():
        print(f"Error: Directory {directory} does not exist", file=sys.stderr)
        sys.exit(1)

    jsonl_files = list(directory_path.glob("*.jsonl"))

    if not jsonl_files:
        print(f"Warning: No .jsonl files found in {directory}", file=sys.stderr)

    for jsonl_file in jsonl_files:
        with open(jsonl_file, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                    key_digest = record.get('key_digest')
                    verdict = record.get('verdict')

                    if key_digest is None:
                        print(f"Warning: Missing key_digest in {jsonl_file}:{line_num}", file=sys.stderr)
                        continue

                    if verdict is None:
                        print(f"Warning: Missing verdict in {jsonl_file}:{line_num}", file=sys.stderr)
                        continue

                    if key_digest in mappings and mappings[key_digest]['verdict'] != verdict:
                        print(f"Warning: Duplicate key_digest '{key_digest}' with different verdicts in {directory}", file=sys.stderr)
                        print(f"  Previous: {mappings[key_digest]['verdict']}, Current: {verdict}", file=sys.stderr)

                    # Store the full record with fields we care about
                    mappings[key_digest] = {
                        'verdict': verdict,
                        'query': record.get('query'),
                        'condition': record.get('condition'),
                        'stacktrace': record.get('stacktrace')
                    }

                except json.JSONDecodeError as e:
                    print(f"Error parsing JSON in {jsonl_file}:{line_num}: {e}", file=sys.stderr)
                    continue

    return mappings


def compare_mappings(dir1, dir2):
    """Compare mappings from two directories and report differences."""
    print(f"Loading mappings from {dir1}...")
    mappings1 = load_mappings(dir1)
    print(f"  Found {len(mappings1)} key_digest entries\n")

    print(f"Loading mappings from {dir2}...")
    mappings2 = load_mappings(dir2)
    print(f"  Found {len(mappings2)} key_digest entries\n")

    # Find keys only in dir1
    only_in_dir1 = set(mappings1.keys()) - set(mappings2.keys())

    # Find keys only in dir2
    only_in_dir2 = set(mappings2.keys()) - set(mappings1.keys())

    # Find keys with different verdicts
    different_verdicts = {}
    for key in set(mappings1.keys()) & set(mappings2.keys()):
        if mappings1[key]['verdict'] != mappings2[key]['verdict']:
            different_verdicts[key] = (mappings1[key], mappings2[key])

    # Report results
    print("=" * 80)
    print("COMPARISON RESULTS")
    print("=" * 80)

    if only_in_dir1:
        print(f"\n❌ Keys only in {dir1}: {len(only_in_dir1)}")
        for key in sorted(only_in_dir1)[:10]:  # Show first 10
            print(f"  - {key}: {mappings1[key]['verdict']}")
        if len(only_in_dir1) > 10:
            print(f"  ... and {len(only_in_dir1) - 10} more")
    else:
        print(f"\n✓ No keys unique to {dir1}")

    if only_in_dir2:
        print(f"\n❌ Keys only in {dir2}: {len(only_in_dir2)}")
        for key in sorted(only_in_dir2)[:10]:  # Show first 10
            print(f"  - {key}: {mappings2[key]['verdict']}")
        if len(only_in_dir2) > 10:
            print(f"  ... and {len(only_in_dir2) - 10} more")
    else:
        print(f"\n✓ No keys unique to {dir2}")

    if different_verdicts:
        print(f"\n❌ Keys with different verdicts: {len(different_verdicts)}")
        for key in sorted(different_verdicts.keys())[:10]:  # Show first 10
            rec1, rec2 = different_verdicts[key]
            print(f"\n  - {key}:")
            print(f"      {Path(dir1).name}: {rec1['verdict']}")
            print(f"      {Path(dir2).name}: {rec2['verdict']}")

            # Print query or condition (only one will be present)
            query_or_condition = rec1.get('query') or rec1.get('condition')
            if query_or_condition:
                field_name = 'query' if rec1.get('query') else 'condition'
                print(f"      {field_name}: {query_or_condition}")

            # Print stacktrace if present
            if rec1.get('stacktrace'):
                print(f"      stacktrace:")
                for frame in rec1['stacktrace']:
                    print(f"        {frame}")

        if len(different_verdicts) > 10:
            print(f"\n  ... and {len(different_verdicts) - 10} more")
    else:
        print("\n✓ All common keys have matching verdicts")

    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    if not only_in_dir1 and not only_in_dir2 and not different_verdicts:
        print("✓ SUCCESS: All key_digest -> verdict mappings match!")
        return 0
    else:
        print("❌ FAILURE: Mappings differ between directories")
        print(f"  - Unique to {Path(dir1).name}: {len(only_in_dir1)}")
        print(f"  - Unique to {Path(dir2).name}: {len(only_in_dir2)}")
        print(f"  - Different verdicts: {len(different_verdicts)}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python compare_verdicts.py <dir1> <dir2>")
        print("\nExample:")
        print("  python compare_verdicts.py oracle-logs replay-gpt-5-low")
        sys.exit(1)

    dir1 = sys.argv[1]
    dir2 = sys.argv[2]

    exit_code = compare_mappings(dir1, dir2)
    sys.exit(exit_code)
