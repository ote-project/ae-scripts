#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: clean_conditioned_queries_jq.sh <input.json> <output.json>" >&2
  exit 1
fi

jq -c --sort-keys 'walk(
      if type == "object" then
        del(.stacktrace, .runId, .colName)
      elif type == "string" then
        sub("^edu\\.berkeley\\.cs\\.netsys\\.policy_extraction\\.path\\.";"")
      else .
      end
    )' "$1" | sort > "$2"
