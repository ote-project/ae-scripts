#!/usr/bin/env bash
set -ex

suffix=${1?param missing - suffix.}

cd "$HOME/dse"
(cd examples; git pull --ff-only)

cd "concolic_driver"
sbt "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/autolab_courses_index.conf $HOME/dse/logs/autolab-courses-index-2r-$suffix --execution-logging=inputs-only"
sbt "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/autolab_assessments_index.conf $HOME/dse/logs/autolab-assessments-index-2r-$suffix --execution-logging=inputs-only"
sbt "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/autolab_assessments_viewGradesheet.conf $HOME/dse/logs/autolab-assessments-gradesheet-2r-$suffix --execution-logging=inputs-only"
sbt "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/autolab_submissions_download.conf $HOME/dse/logs/autolab-submissions-download-2r-$suffix --execution-logging=inputs-only"
sbt "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutions $HOME/dse/examples/autolab_metrics_get_num_pending_instances.conf $HOME/dse/logs/autolab-metrics-pending-2r-$suffix --execution-logging=inputs-only"

