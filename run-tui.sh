#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# ──  CLI options  ─────────────────────────────────────────────────────────────
###############################################################################
do_pull=true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-pull)
            do_pull=false
            shift
            ;;
        --help|-h)
            cat <<'USAGE'
Usage: run-tui.sh [--no-pull]

Options:
  --no-pull   Skip the git pull steps before running experiments.
USAGE
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

###############################################################################
# ──  Interactive front-end  ──────────────────────────────────────────────────
###############################################################################
command -v gum >/dev/null ||
    { echo "❌  Please install 'gum' first."; exit 1; }

suffix=$(gum input --value "$(date +%Y%m%d-%H%M%S)" \
                   --prompt "Suffix for log-files ➜ ")
[[ -z "$suffix" ]] && { echo "❌  Suffix cannot be empty."; exit 1; }

diaspora_experiments=(
  "diaspora_comments_index"
  "diaspora_conversations_index"
  "diaspora_notifications_index"
  "diaspora_people_show"
  "diaspora_posts_show"
  "diaspora_people_stream"
)

autolab_experiments=(
  "autolab_courses_index"
  "autolab_assessments_index"
  "autolab_assessments_viewGradesheet"
  "autolab_submissions_download"
  "autolab_metrics_get_num_pending_instances"
)

apps_selected=$(printf '%s\n' diaspora autolab |
                gum choose --no-limit --header "Select applications to run")

if [[ -z "$apps_selected" ]]; then
  echo "No applications selected – aborting."; exit 0;
fi

IFS=$'\n' readarray -t apps <<<"$apps_selected"
experiments=()
for app in "${apps[@]}"; do
  app_var="${app}_experiments"
  if ! declare -p "$app_var" >/dev/null 2>&1; then
    echo "❌  Unknown application '$app'." >&2
    exit 1
  fi

  declare -n app_array="$app_var"
  selection=$(printf '%s\n' "${app_array[@]}" |
              gum choose --no-limit \
                         --header "Select ${app^} experiments (Esc = skip)" \
                         --selected "*")

  [[ -z "$selection" ]] && continue
  IFS=$'\n' readarray -t chosen <<<"$selection"
  experiments+=("${chosen[@]}")
done

if [[ "${#experiments[@]}" -eq 0 ]]; then
  echo "Nothing selected – aborting."; exit 0;
fi

memory=$(gum input --value "20480" \
                   --prompt "Maximum heap (MB) ➜ ")

logging=$(printf '%s\n' inputs-only full none |
          gum choose --cursor "•" --header "Select execution-logging mode")

extra_opts=$(gum input --prompt "Extra options for ExploreExecutions (blank = none) ➜ ")

###############################################################################
# ──  Run experiments  ────────────────────────────────────────────────────────
###############################################################################
cd "$HOME/dse"
if [[ "$do_pull" == true ]]; then
    (cd examples; git pull --ff-only)
fi

cd "concolic_driver"
if [[ "$do_pull" == true ]]; then
    git pull --ff-only
fi
for exp in "${experiments[@]}"; do
    conf="$HOME/dse/examples/${exp}.conf"
    log="$HOME/dse/logs/${exp//_/-}-2r-${suffix}"

    echo "▶︎  Running $exp…"
    sbt -mem "$memory" \
        "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions \
               ${conf} ${log} --execution-logging=${logging} ${extra_opts}"
done
