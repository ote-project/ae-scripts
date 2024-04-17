#!/usr/bin/env bash
set -e

gcloud_path=${1?param missing - gcloud path.}

# https://stackoverflow.com/questions/4632028/how-to-create-a-temporary-directory
WORK_DIR="$(mktemp -d)"
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
  (( D > 0 )) && printf '%d days ' $D
  (( H > 0 )) && printf '%d hours ' $H
  (( M > 0 )) && printf '%d minutes ' $M
  (( D > 0 || H > 0 || M > 0 )) && printf 'and '
  printf '%d seconds\n' $S
}

# register the cleanup function to be called on the EXIT signal
trap cleanup EXIT

cd "$WORK_DIR"
gcloud --quiet storage cat "gs://$gcloud_path/metrics/edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutionsImpl.runs.csv" > ./runs.csv

exploration_dur_s="$(csvq --without-header 'select max(t) - min(t)' < ./runs.csv)"
num_paths="$(csvq --without-header "select max(\`count\`)" < ./runs.csv)"

printf "Run dir:\t%s\n" "$(basename "$gcloud_path")"

printf "Num paths:\t%d\n" "$num_paths"

printf "Exploration time:\t"
displaytime "$exploration_dur_s"
echo

gcloud --quiet storage cat "gs://$gcloud_path/annotated-paths/generate-cqs.log" > ./generate-cqs.log
num_cqs_start="$(sed -n 's/.*Converted to \([0-9]\+\) conditioned queries\..*/\1/p' ./generate-cqs.log | tail -1)"
num_cqs_end="$(sed -n 's/.*There are \([0-9]\+\) conditioned queries after removing subsumed\..*/\1/p' ./generate-cqs.log | tail -1)"
printf "CQs:\t%d -> %d\n" "$num_cqs_start" "$num_cqs_end"

post_processing_dur_s="$(gcloud --quiet storage cat "gs://$gcloud_path/annotated-paths/post-processing-time-sec.txt")"
printf "Per-handler view-generation time:\t"
displaytime "${post_processing_dur_s%.*}"
