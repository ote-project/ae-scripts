#!/usr/bin/env python3
import json
import sys
from typing import List, Tuple


def perform_rewrite(rewrites: List[Tuple[str, str]]) -> None:
    result = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        cq = json.loads(line)

        for (rewrite_from, rewrite_to) in rewrites:
            if cq["query"] == rewrite_from:
                result.append({
                    **cq,
                    "query": rewrite_to,
                })
                break
        else:
            result.append(cq)

    for cq in result:
        print(json.dumps(cq).replace("\n", ""))

