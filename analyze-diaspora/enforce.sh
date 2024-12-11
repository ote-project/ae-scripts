#!/usr/bin/env bash
set -ex

views_path=${1?param missing - views_path.}
"$HOME/dse/scripts/enforce.py" "$HOME/dse/app-policies-amended/diaspora" "jdbc:mysql://localhost:3306/diaspora_test?allowPublicKeyRetrieval=true&useSSL=false" "diaspora_test" "diaspora" "12345678" "$views_path"
