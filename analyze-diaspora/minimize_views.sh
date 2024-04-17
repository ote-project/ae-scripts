#!/usr/bin/env bash
set -ex

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
cd "$SCRIPT_DIR/.."

for d in diaspora-*; do
  cat "$d/views.sql" | /home/ubuntu/dse/scripts/filter_unsupported_views.sh | ~/dse/scripts/remove_subsumed.py /home/ubuntu/dse/diaspora/policy "jdbc:mysql://localhost:3306/diaspora_test?allowPublicKeyRetrieval=true&useSSL=false" diaspora_test diaspora 12345678 > "$d/views-minimized.sql"
done

cat **/views-minimized.sql | /home/ubuntu/dse/scripts/filter_unsupported_views.sh | ~/dse/scripts/remove_subsumed.py /home/ubuntu/dse/diaspora/policy "jdbc:mysql://localhost:3306/diaspora_test?allowPublicKeyRetrieval=true&useSSL=false" diaspora_test diaspora 12345678 > "$SCRIPT_DIR/all-minimized.sql"
