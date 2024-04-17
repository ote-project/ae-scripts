#!/usr/bin/env python3
import json
import sys
from typing import Optional


REWRITES = [
    ("SELECT COUNT(*) FROM `submissions` WHERE `submissions`.`assessment_id` = ? AND `submissions`.`course_user_datum_id` = ?",
     "SELECT `submissions`.`id` FROM `submissions` WHERE `submissions`.`assessment_id` = ? AND `submissions`.`course_user_datum_id` = ?"),
    ("SELECT COUNT(DISTINCT `watchlist_instances`.`course_user_datum_id`) FROM `watchlist_instances` WHERE `watchlist_instances`.`course_id` = ? AND `watchlist_instances`.`status` = ?",
     "SELECT `watchlist_instances`.`course_user_datum_id` FROM `watchlist_instances` WHERE `watchlist_instances`.`course_id` = ? AND `watchlist_instances`.`status` = ?"),
]


def main() -> None:
    result = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        cq = json.loads(line)

        for (rewrite_from, rewrite_to) in REWRITES:
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


if __name__ == "__main__":
    main()
