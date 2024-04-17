#!/usr/bin/env bash
set -ex

if [ $# -lt 2 ]; then
  echo 1>&2 "$0: not enough arguments"
  exit 2
elif [ $# -gt 2 ]; then
  echo 1>&2 "$0: too many arguments"
  exit 2
fi

config_file="$(realpath "$1")"
run_dir="$(realpath "$2")"

cd $HOME/dse/concolic_driver
# sbt -mem 102400 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.AnnotatePaths $config_file $run_dir/paths $run_dir/annotated-paths -j46"
sbt -mem 102400 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.GenerateConditionedQueries $config_file $run_dir/annotated-paths"
sbt -mem 102400 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ConvertToSqlViews $config_file $run_dir/annotated-paths"

