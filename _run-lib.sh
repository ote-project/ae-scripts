#!/usr/bin/env bash
# Shared library for run-*.sh scripts. Source, don't execute.
#
# Provides experiment arrays and a `run_experiments` function. The function
# reads these globals from the caller:
#   experiments  (array)  - experiment names to run
#   suffix       (string) - log-file suffix
#   memory       (string) - sbt heap in MB
#   logging      (string) - --execution-logging mode
#   extra_opts   (string) - extra flags appended to ExploreExecutions
#   do_pull      (bool)   - if "true", git-pull app-config and concolic_driver

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
  "autolab_assessments_show"
)

theodinproject_experiments=(
  "theodinproject_sitemap_index"
  "theodinproject_paths_index"
  "theodinproject_users_show"
  "theodinproject_project-submissions_index"
  "theodinproject_courses_show"
  "theodinproject_lessons_show"
)

run_experiments() {
    cd "$HOME/dse"
    if [[ "$do_pull" == true ]]; then
        (cd app-config; git pull --ff-only)
    fi

    cd "concolic_driver"
    if [[ "$do_pull" == true ]]; then
        git pull --ff-only
    fi
    for exp in "${experiments[@]}"; do
        local conf="$HOME/dse/app-config/${exp}.conf"
        local log="$HOME/dse/logs/${exp//_/-}-2r-${suffix}"

        echo "▶︎  Running $exp…"
        sbt -mem "$memory" \
            "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions \
                   ${conf} ${log} --execution-logging=${logging} --silence-path-warnings ${extra_opts}"
    done
}
