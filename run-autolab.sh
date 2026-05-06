#!/usr/bin/env bash
set -ex

suffix=${1?param missing - suffix.}

cd "$HOME/dse"
(cd app-config; git pull --ff-only)
export DSE_INCLUDE_STACKTRACE=true
cd "concolic_driver"
sbt "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/app-config/autolab_courses_index.conf $HOME/dse/logs/autolab-courses-index-2r-$suffix --execution-logging=inputs-only --silence-path-warnings"
sbt "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/app-config/autolab_assessments_index.conf $HOME/dse/logs/autolab-assessments-index-2r-$suffix --execution-logging=inputs-only --silence-path-warnings"
sbt "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/app-config/autolab_assessments_viewGradesheet.conf $HOME/dse/logs/autolab-assessments-gradesheet-2r-$suffix --execution-logging=inputs-only --silence-path-warnings"
sbt "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/app-config/autolab_submissions_download.conf $HOME/dse/logs/autolab-submissions-download-2r-$suffix --execution-logging=inputs-only --silence-path-warnings"
sbt "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/app-config/autolab_metrics_get_num_pending_instances.conf $HOME/dse/logs/autolab-metrics-pending-2r-$suffix --execution-logging=inputs-only --silence-path-warnings"

