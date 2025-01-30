#!/usr/bin/env bash
set -ex

views_path=${1?param missing - views_path.}
"$HOME/dse/scripts/format_views.py" | \
  "$HOME/dse/scripts/enforce.py" \
    "$HOME/dse/app-policies-amended/theodinproject" \
    "jdbc:mysql://localhost:3306/theodinproject_test?allowPublicKeyRetrieval=true&useSSL=false" \
    "theodinproject_test" "theodinproject" "12345678" \
    "$views_path"
