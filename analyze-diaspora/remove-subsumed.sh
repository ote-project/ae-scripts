#!/usr/bin/env bash
set -ex

suffix=${1?param missing - suffix.}

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
. "$SCRIPT_DIR/helper.sh"

for d in $HOME/dse/logs/diaspora-*$suffix; do
    START=$(date +%s.%N)
    cat "$d/annotated-paths/views.sql" | $HOME/dse/scripts/filter_unsupported_views.sh | remove_subsumed > "$d/annotated-paths/views-minimized.sql"
    END=$(date +%s.%N)
    DIFF=$(echo "$END - $START" | bc)
    echo "$DIFF" > "$d/annotated-paths/remove-subsumed-time-sec.txt"
done

policy_dir="$HOME/dse/logs/diaspora-$suffix-policy"
mkdir -p "$policy_dir"

START=$(date +%s.%N)
cat $HOME/dse/logs/diaspora-*$suffix/annotated-paths/views-minimized.sql | \
    $HOME/dse/scripts/filter_unsupported_views.sh | \
    remove_subsumed >"$policy_dir/all-minimized.sql"
"$HOME/dse/scripts/pretty_print_views.py" <"$policy_dir/all-minimized.sql" >"$policy_dir/all-minimized-pretty.sql"
END=$(date +%s.%N)
DIFF=$(echo "$END - $START" | bc)
echo "$DIFF" > "$policy_dir/remove-subsumed-time-sec.txt"

