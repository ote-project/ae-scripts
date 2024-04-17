#!/usr/bin/env python3
"""
This script takes the conditioned queries produced from the diaspora-people-show trace and broadens it by
removing the LEFT OUTER JOINs.  Works only for the particular LEFT OUTER JOINs in the diaspora-people-show
trace.
"""
import json
import sys
from typing import Optional


LOOK_FOR_QUERY = ("SELECT COUNT(*) FROM (SELECT DISTINCT photos.* FROM `photos` "
                  "LEFT OUTER JOIN share_visibilities ON share_visibilities.shareable_id = photos.id "
                  "AND share_visibilities.shareable_type = 'Photo' "
                  "WHERE `photos`.`author_id` = ? AND (`share_visibilities`.`user_id` = ? OR `photos`.`public` = 1) "
                  "AND `photos`.`created_at` < ? AND `photos`.`pending` = ? "
                  "ORDER BY photos.created_at DESC, created_at DESC) subquery_for_count")

REWRITE_LEFT = ("SELECT `photos`.* FROM `photos` WHERE `photos`.`author_id` = ? AND `photos`.`public` = 1 "
                "AND `photos`.`created_at` < ? AND `photos`.`pending` = ?")
REWRITE_LEFT_PARAMS = (0, 2, 3)

REWRITE_RIGHT = ("SELECT `photos`.* FROM `photos` "
                 "INNER JOIN `share_visibilities` ON share_visibilities.shareable_id = photos.id "
                 "AND share_visibilities.shareable_type = 'Photo' "
                 "WHERE `photos`.`author_id` = ? "
                 "AND `share_visibilities`.`user_id` = ? "
                 "AND `photos`.`created_at` < ? AND `photos`.`pending` = ?")
REWRITE_RIGHT_PARAMS = (0, 1, 2, 3)


def main() -> None:
    result = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        cq = json.loads(line)
        if cq["query"] != LOOK_FOR_QUERY:
            result.append(cq)
            continue

        result.append({
            **cq,
            "query": REWRITE_LEFT,
            "params": [cq["params"][i] for i in REWRITE_LEFT_PARAMS],
        })
        result.append({
            **cq,
            "query": REWRITE_RIGHT,
            "params": [cq["params"][i] for i in REWRITE_RIGHT_PARAMS],
        })

    for cq in result:
        print(json.dumps(cq).replace("\n", ""))


if __name__ == "__main__":
    main()
