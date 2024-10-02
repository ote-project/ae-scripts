#!/usr/bin/env bash
set -ex

APP="theodinproject"
DATABASE="theodinproject_test"
JDBC_URL="jdbc:mysql://localhost:3306/$DATABASE?allowPublicKeyRetrieval=true&useSSL=false"
DATABASE_USER="theodinproject"
DATABASE_PASSWORD="12345678"

suffix=${1?param missing - suffix.}

remove_subsumed() {
    "$HOME/dse/scripts/remove_subsumed.py" "$HOME/dse/app-policies-amended/$APP" \
      "$JDBC_URL" \
      "$DATABASE" "$DATABASE_USER" "$DATABASE_PASSWORD"
}

for d in "$HOME"/dse/logs/"$APP"-*"$suffix"; do
    echo "$d"

    START=$(date +%s.%N)
    <"$d/annotated-paths/views.sql" \
      "$HOME"/dse/scripts/filter_unsupported_views.sh | remove_subsumed \
      >"$d/annotated-paths/views-minimized.sql"
    END=$(date +%s.%N)
    DIFF=$(echo "$END - $START" | bc)

    echo "$DIFF" > "$d/annotated-paths/remove-subsumed-time-sec.txt"
done

policy_dir="$HOME/dse/logs/$APP-$suffix-policy"
mkdir -p "$policy_dir"

START=$(date +%s.%N)
cat "$HOME"/dse/logs/"$APP"-*"$suffix"/annotated-paths/views-minimized.sql | \
    "$HOME/dse/scripts/filter_unsupported_views.sh" | \
    remove_subsumed | \
    "$HOME/dse/scripts/pretty_print_views.py" >"$policy_dir/all-minimized.sql"
END=$(date +%s.%N)
DIFF=$(echo "$END - $START" | bc)
echo "$DIFF" > "$policy_dir/remove-subsumed-time-sec.txt"
