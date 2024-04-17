#!/usr/bin/env python3
import json
import sys
from typing import Optional


REWRITES = [
    ("SELECT COUNT(*) FROM `notifications` WHERE `notifications`.`recipient_id` = ? AND `notifications`.`unread` = ?",
     "SELECT `notifications`.`id` FROM `notifications` WHERE `notifications`.`recipient_id` = ? AND `notifications`.`unread` = ?"),
    ("SELECT COUNT(*) FROM `notifications` WHERE `notifications`.`recipient_id` = ?",
     "SELECT `notifications`.`id` FROM `notifications` WHERE `notifications`.`recipient_id` = ?"),
    ("SELECT COUNT(*) FROM `photos` WHERE `photos`.`author_id` = ? AND `photos`.`created_at` < ? AND `photos`.`pending` = ?",
     "SELECT `photos`.`id` FROM `photos` WHERE `photos`.`author_id` = ? AND `photos`.`created_at` < ? AND `photos`.`pending` = ?"),
    ("SELECT COUNT(*) FROM `contacts` WHERE `contacts`.`user_id` = ? AND `contacts`.`receiving` = ?",
     "SELECT `contacts`.`id` FROM `contacts` WHERE `contacts`.`user_id` = ? AND `contacts`.`receiving` = ?"),
    ("SELECT COUNT(*) FROM `aspects` WHERE `aspects`.`user_id` = ?",
     "SELECT `aspects`.`id` FROM `aspects` WHERE `aspects`.`user_id` = ?"),
    ("SELECT COUNT(*) FROM (SELECT DISTINCT `conversation_visibilities`.`id` FROM `conversation_visibilities` LEFT OUTER JOIN `conversations` ON `conversations`.`id` = `conversation_visibilities`.`conversation_id` WHERE `conversation_visibilities`.`person_id` = ?) subquery_for_count",
     "SELECT `conversation_visibilities`.`id` FROM `conversation_visibilities` INNER JOIN `conversations` ON `conversations`.`id` = `conversation_visibilities`.`conversation_id` WHERE `conversation_visibilities`.`person_id` = ?"),
    ("SELECT SUM(`conversation_visibilities`.`unread`) FROM `conversation_visibilities` WHERE `conversation_visibilities`.`person_id` = ?",
     "SELECT `conversation_visibilities`.`id`, `conversation_visibilities`.`unread` FROM `conversation_visibilities` WHERE `conversation_visibilities`.`person_id` = ?"),
    ("SELECT SUM(`poll_answers`.`vote_count`) FROM `poll_answers` WHERE `poll_answers`.`poll_id` = ?",
     "SELECT `poll_answers`.`id`, `poll_answers`.`vote_count` FROM `poll_answers` WHERE `poll_answers`.`poll_id` = ?"),
    ("SELECT COUNT(*) FROM `people` INNER JOIN `conversation_visibilities` ON `people`.`id` = `conversation_visibilities`.`person_id` WHERE `conversation_visibilities`.`conversation_id` = ?",
     "SELECT `people`.`id` FROM `people` INNER JOIN `conversation_visibilities` ON `people`.`id` = `conversation_visibilities`.`person_id` WHERE `conversation_visibilities`.`conversation_id` = ?"),
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
