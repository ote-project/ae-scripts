#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# ──  Interactive front-end  ──────────────────────────────────────────────────
###############################################################################
command -v gum >/dev/null ||
    { echo "❌  Please install 'gum' first."; exit 1; }

suffix=$(gum input --value "$(date +%Y%m%d-%H%M%S)" \
                   --prompt "Suffix for log-files ➜ ")
[[ -z "$suffix" ]] && { echo "❌  Suffix cannot be empty."; exit 1; }

all_experiments=(
  "diaspora_comments_index"
  "diaspora_conversations_index"
  "diaspora_notifications_index"
  "diaspora_people_show"
  "diaspora_posts_show"
  "diaspora_people_stream"
)
selected=$(printf '%s\n' "${all_experiments[@]}" |
           gum choose --no-limit --header "Select experiments to run" --selected "*")

if [[ -z "$selected" ]]; then
  echo "Nothing selected – aborting."; exit 0;
fi

IFS=$'\n' readarray -t experiments <<<"$selected"

memory=$(gum input --value "20480" \
                   --prompt "Maximum heap (MB) ➜ ")

logging=$(printf '%s\n' inputs-only full none |
          gum choose --cursor "•" --header "Select execution-logging mode")

extra_opts=$(gum input --prompt "Extra options for ExploreExecutions (blank = none) ➜ ")

###############################################################################
# ──  Run experiments  ────────────────────────────────────────────────────────
###############################################################################
cd "$HOME/dse"
(cd examples; git pull --ff-only)

cd "concolic_driver"
for exp in "${experiments[@]}"; do
    conf="$HOME/dse/examples/${exp}.conf"
    log="$HOME/dse/logs/${exp//_/-}-2r-${suffix}"

    echo "▶︎  Running $exp…"
    sbt -mem "$memory" \
        "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions \
               ${conf} ${log} --execution-logging=${logging} ${extra_opts}"
done
