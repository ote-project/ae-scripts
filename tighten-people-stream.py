#!/usr/bin/env python3
"""
This script takes the conditioned queries produced from the diaspora-people-stream trace and tightens it by removing
the LEFT OUTER JOINs.  A conditioned query with a LEFT OUTER JOIN in the query issued will be removed altogether.
A conditioned query with a LEFT OUTER JOIN in a single condition will be split up into two queries: this makes the
condition stricter than before.

Works only for the particular LEFT OUTER JOINs in the diaspora-people-stream trace.
"""
import json
import sys
from typing import Optional


LOOK_FOR_QUERY = ("SELECT  DISTINCT posts.* FROM `posts` "
                  "LEFT OUTER JOIN share_visibilities ON share_visibilities.shareable_id = posts.id "
                  "AND share_visibilities.shareable_type = 'Post' "
                  "WHERE `posts`.`author_id` = ? "
                  "AND (`share_visibilities`.`user_id` = ? OR `posts`.`public` = 1) "
                  "AND `posts`.`created_at` < ? AND `posts`.`type` IN (?, ?) "
                  "ORDER BY posts.created_at desc, posts.created_at DESC, posts.id DESC LIMIT ?")

REWRITE_LEFT = ("SELECT `posts`.* FROM `posts` WHERE `posts`.`author_id` = ? AND `posts`.`public` = 1 "
                "AND `posts`.`created_at` < ? AND `posts`.`type` IN (?, ?)")
REWRITE_LEFT_PARAMS = (0, 2, 3, 4)

REWRITE_RIGHT = ("SELECT `posts`.* FROM `posts` "
                 "INNER JOIN `share_visibilities` ON share_visibilities.shareable_id = posts.id "
                 "AND share_visibilities.shareable_type = 'Post' "
                 "WHERE `posts`.`author_id` = ? "
                 "AND `share_visibilities`.`user_id` = ? "
                 "AND `posts`.`created_at` < ? AND `posts`.`type` IN (?, ?)")
REWRITE_RIGHT_PARAMS = (0, 1, 2, 3, 4)


def find_target_condition(conditions: dict[list]) -> Optional[int]:
    for (i, cond) in enumerate(conditions):
        elem = cond["elem"]
        if elem["$type"] != "SqlQueryDecl":
            continue
        if elem["query"].strip() != LOOK_FOR_QUERY:
            continue
        return i
    return None


def rewrite(cond: dict, to_query: str, to_params_indices: tuple[int, ...]) -> dict:
    elem = cond["elem"]
    assert elem["$type"] == "SqlQueryDecl"
    assert all(0 <= i < len(elem["params"]) for i in to_params_indices)

    new_elem = {
        "$type": "SqlQueryDecl",
        "qIdx": elem["qIdx"],
        "query": to_query,
        "params": [
            elem["params"][i] for i in to_params_indices
        ],
    }

    return {
        "elem": new_elem,
        "vacuousness": cond["vacuousness"],
    }


def substitute_condition(cq: dict, i: int, new_cond: dict) -> dict:
    assert 0 <= i < len(cq["conditions"])
    return {
        **cq,
        "conditions": cq["conditions"][:i] + [new_cond] + cq["conditions"][i + 1:],
    }


def main() -> None:
    result = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        cq = json.loads(line)
        if "LEFT OUTER JOIN" in cq["query"]:
            continue

        i = find_target_condition(cq["conditions"])
        if i is None:
            result.append(cq)
            continue

        # Split condition into two.
        cond = cq["conditions"][i]
        new_cond1 = rewrite(cond, REWRITE_LEFT, REWRITE_LEFT_PARAMS)
        result.append(substitute_condition(cq, i, new_cond1))

        new_cond2 = rewrite(cond, REWRITE_RIGHT, REWRITE_RIGHT_PARAMS)
        result.append(substitute_condition(cq, i, new_cond2))

    for cq in result:
        print(json.dumps(cq).replace("\n", ""))


if __name__ == "__main__":
    main()
