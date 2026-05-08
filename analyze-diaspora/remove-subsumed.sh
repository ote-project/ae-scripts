#!/usr/bin/env bash
set -ex

skip_individuals=false
skip_final=false

while [[ "${1:-}" == --* ]]; do
    case "$1" in
        --skip-individuals) skip_individuals=true ;;
        --skip-final) skip_final=true ;;
        *) echo "Unknown flag: $1" >&2; exit 1 ;;
    esac
    shift
done

suffix=${1?param missing - suffix.}
analysis_id=${2?param missing - analysis_id.}

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
. "$SCRIPT_DIR/helper.sh"

if ! $skip_individuals; then
    for d in "$HOME"/dse/logs/diaspora-*"$suffix"; do
        analysis_dir="$d/analysis-$analysis_id"
        START=$(date +%s.%N)
        <"$analysis_dir/views.sql" "$HOME/dse/scripts/filter_unsupported_views.sh" | \
            remove_subsumed > "$analysis_dir/views-minimized.sql"
        END=$(date +%s.%N)
        DIFF=$(echo "$END - $START" | bc)
        echo "$DIFF" > "$analysis_dir/remove-subsumed-time-sec.txt"
    done
fi

if ! $skip_final; then
    policy_dir="$HOME/dse/logs/diaspora-$suffix-policy-$analysis_id"
    mkdir -p "$policy_dir"

    START=$(date +%s.%N)
    cat "$HOME"/dse/logs/diaspora-*"$suffix"/analysis-"$analysis_id"/views-minimized.sql | \
        "$HOME/dse/scripts/filter_unsupported_views.sh" | \
        remove_subsumed >"$policy_dir/all-minimized.sql"
    "$HOME/dse/scripts/pretty_print_views.py" <"$policy_dir/all-minimized.sql" >"$policy_dir/all-minimized-pretty.sql"
    END=$(date +%s.%N)
    DIFF=$(echo "$END - $START" | bc)
    echo "$DIFF" > "$policy_dir/remove-subsumed-time-sec.txt"
fi

