#!/usr/bin/env bash
# Comparison between date and timestamp is not supported by Blockaid.
# Assumes this pattern does not appear in a negation.
# Writing `1=1` instead of `TRUE` because Blockaid doesn't support the latter generally...
sed 's/_NOW \(<\|<=\|>\|>=\) `courses[0-9]*`\.`\(start\|end\)_date`/1=1/g'
