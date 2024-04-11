#!/usr/bin/env python3
from collections import defaultdict
import re
import sys

def main():
    lines = list(sys.stdin)[::-1]
    mismatch_pairs = defaultdict(list)
    while lines:
        if (result := re.search(r"Path mismatch \(runId = (\d+)\)", lines.pop())) is None:
            continue
        runId = result.group(1)

        assert lines.pop().startswith("Inverted path:")
        inverted_path = []
        while lines[-1].startswith(" "):
            inverted_path.append(lines.pop().strip())

        assert lines.pop().startswith("Actual path:")
        actual_path = []
        while lines and lines[-1].startswith(" "):
            actual_path.append(lines.pop().strip())

        for inverted_elem, actual_elem in zip(inverted_path, actual_path):
            if inverted_elem != actual_elem:
                mismatch_pairs[(inverted_elem, actual_elem)].append(runId)
                break
        else:
            assert False, "no mismatch: " + runId

    for (inverted_elem, actual_elem), runIds in mismatch_pairs.items():
        print(inverted_elem)
        print(actual_elem)
        print(", ".join(str(i) for i in runIds))
        print()


if __name__ == "__main__":
    main()

