#!/usr/bin/env bash
# This particular LEFT OUTER JOIN can be turned into an INNER JOIN equivalently.
sed 's|SELECT COUNT(\*) FROM `notifications`|SELECT `notifications`.`id` FROM `notifications`|g' | \
    sed 's|SELECT COUNT(\*) FROM `contacts`|SELECT `contacts`.`id` FROM `contacts`|g' | \
    sed 's|SELECT COUNT(\*) FROM `photos`|SELECT `photos`.`id` FROM `photos`|g' | \
    sed 's|SELECT COUNT(\*) FROM `aspects`|SELECT `aspects`.`id` FROM `aspects`|g'
