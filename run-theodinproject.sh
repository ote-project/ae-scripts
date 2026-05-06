#!/usr/bin/env bash
set -ex

APP="theodinproject"
HANDLERS=(
  "sitemap_index"
  "paths_index"
  "users_show"
  "project-submissions_index"
  "courses_show"
  "lessons_show"
)

suffix=${1?param missing - suffix.}

cd "$HOME/dse"
(cd app-config; git pull --ff-only)

cd "concolic_driver"
for handler in "${HANDLERS[@]}"; do
  DSE_TRACK_LT=1 sbt -mem 20480 \
    "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions
      ${HOME}/dse/app-config/${APP}_${handler}.conf
      ${HOME}/dse/logs/${APP}-${handler}-${suffix}
      --execution-logging=inputs-only"
done

