#!/usr/bin/env python3
import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from rewrite_aggs import perform_rewrite

REWRITES = [
    ("SELECT COUNT(*) FROM `lesson_completions` WHERE `lesson_completions`.`user_id` = ? AND `lesson_completions`.`lesson_id` = ?",
     "SELECT `lesson_completions`.`id` FROM `lesson_completions` WHERE `lesson_completions`.`user_id` = ? AND `lesson_completions`.`lesson_id` = ?"),
    ("SELECT COUNT(*) FROM `votes` WHERE `votes`.`votable_id` = ? AND `votes`.`votable_type` = ?",
     "SELECT `votes`.`id` FROM `votes` WHERE `votes`.`votable_id` = ? AND `votes`.`votable_type` = ?"),
    ("SELECT COUNT(*) FROM `lesson_completions` WHERE `lesson_completions`.`user_id` = ? AND 1=0",
     "SELECT `lesson_completions`.`id` FROM `lesson_completions` WHERE `lesson_completions`.`user_id` = ? AND 1=0"),
    ("SELECT COUNT(*) FROM `project_submissions` WHERE `project_submissions`.`lesson_id` = ? AND `project_submissions`.`user_id` != ? AND `project_submissions`.`is_public` = ? AND `project_submissions`.`banned` = ? AND `project_submissions`.`discarded_at` IS NULL",
     "SELECT `project_submissions`.`id` FROM `project_submissions` WHERE `project_submissions`.`lesson_id` = ? AND `project_submissions`.`user_id` != ? AND `project_submissions`.`is_public` = ? AND `project_submissions`.`banned` = ? AND `project_submissions`.`discarded_at` IS NULL"),
]


if __name__ == '__main__':
    perform_rewrite(REWRITES)

