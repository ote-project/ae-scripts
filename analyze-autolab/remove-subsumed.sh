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

remove_subsumed() {
    "$HOME/dse/scripts/remove_subsumed.py" \
      "$HOME/dse/app-policies-amended/Autolab" \
      "jdbc:mysql://localhost:3306/autolab_test?allowPublicKeyRetrieval=true&useSSL=false" \
      "autolab_test" "autolab" "12345678"
}

if ! $skip_individuals; then
    for d in "$HOME"/dse/logs/autolab-*"$suffix"; do
        analysis_dir="$(realpath "$d/analysis-$analysis_id")"

        START=$(date +%s.%N)
        <"$analysis_dir/views.sql" \
            "$HOME/dse/scripts/analyze-autolab/broaden.sh" | \
            "$HOME/dse/scripts/filter_unsupported_views.sh" | \
            remove_subsumed > "$analysis_dir/views-minimized.sql"
        END=$(date +%s.%N)
        DIFF=$(echo "$END - $START" | bc)
        echo "$DIFF" > "$analysis_dir/remove-subsumed-time-sec.txt"
    done
fi

if ! $skip_final; then
    policy_dir="$HOME/dse/logs/autolab-$suffix-policy-$analysis_id"
    mkdir -p "$policy_dir"

    START=$(date +%s.%N)
    cat "$HOME"/dse/logs/autolab-*"$suffix"/analysis-"$analysis_id"/views-minimized.sql | \
        "$HOME"/dse/scripts/filter_unsupported_views.sh | \
        remove_subsumed >"$policy_dir/all-minimized.sql"
    END=$(date +%s.%N)
    DIFF=$(echo "$END - $START" | bc)
    echo "$DIFF" > "$policy_dir/remove-subsumed-time-sec.txt"
fi

