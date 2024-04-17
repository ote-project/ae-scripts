#!/usr/bin/env bash
set -e

gcloud_path=${1?param missing - gcloud path.}

# https://stackoverflow.com/questions/4632028/how-to-create-a-temporary-directory
WORK_DIR=`mktemp -d`
if [[ ! "$WORK_DIR" || ! -d "$WORK_DIR" ]]; then
  echo "Could not create temp dir"
  exit 1
fi

# deletes the temp directory
function cleanup {      
  rm -rf "$WORK_DIR"
}

# https://unix.stackexchange.com/questions/27013/displaying-seconds-as-days-hours-mins-seconds
function displaytime {
  local T=$1
  local D=$((T/60/60/24))
  local H=$((T/60/60%24))
  local M=$((T/60%60))
  local S=$((T%60))
  (( $D > 0 )) && printf '%d days ' $D
  (( $H > 0 )) && printf '%d hours ' $H
  (( $M > 0 )) && printf '%d minutes ' $M
  (( $D > 0 || $H > 0 || $M > 0 )) && printf 'and '
  printf '%d seconds\n' $S
}

# register the cleanup function to be called on the EXIT signal
trap cleanup EXIT

cd "$WORK_DIR"
gcloud --quiet storage cp "gs://$gcloud_path/metrics/edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutionsImpl.runs.csv" ./runs.csv

dur_s="$(cat ./runs.csv | csvq --without-header 'select max(t) - min(t)')"
num_paths="$(cat ./runs.csv | csvq --without-header 'select max(`count`)')"

printf "Run dir:\t$(basename $gcloud_path)\n"

printf "Duration:\t"
displaytime "$dur_s"

printf "Num paths:\t%d\n" "$num_paths"
