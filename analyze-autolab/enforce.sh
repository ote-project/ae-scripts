#!/usr/bin/env bash
set -ex

views_path=${1?param missing - views_path.}
"$HOME/dse/scripts/enforce.py" "$HOME/dse/app-policies-amended/Autolab" "jdbc:mysql://localhost:3306/autolab_test?allowPublicKeyRetrieval=true&useSSL=false" "autolab_test" "autolab" "12345678" "$views_path"
