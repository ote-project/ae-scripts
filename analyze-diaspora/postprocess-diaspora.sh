#!/usr/bin/env bash
set -ex

# Default memory for sbt (in MB)
MEM_MB=131072

# Parse options: allow optional -m <MB>
while getopts ":m:" opt; do
  case $opt in
    m)
      MEM_MB="$OPTARG"
      ;;
    \?)
      echo "Usage: $(basename "$0") [-m MEM_MB] suffix" >&2
      exit 1
      ;;
  esac
done

shift $((OPTIND-1))

suffix=${1?param missing - suffix.}

cd "$HOME/dse"
(cd examples; git pull --ff-only)

config_file="$HOME/dse/examples/diaspora_posts_show.conf"
for p in $HOME/dse/logs/diaspora-*$suffix; do
    START=$(date +%s.%N)

    paths_dir="$(realpath "$p/annotated-paths")"
    rm -f $paths_dir/original-conditioned-queries.json
    (cd $HOME/dse/concolic_driver; sbt -mem "$MEM_MB" "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.GenerateConditionedQueries $config_file $paths_dir")

    (cd $paths_dir;
        if [ ! -f original-conditioned-queries.json ]; then
            mv conditioned-queries.json original-conditioned-queries.json; 
        fi;
        cat original-conditioned-queries.json | \
            $HOME/dse/scripts/analyze-diaspora/broaden-people-show.py |
            $HOME/dse/scripts/analyze-diaspora/tighten-people-stream.py |
            $HOME/dse/scripts/analyze-diaspora/rewrite-aggs.py |
            $HOME/dse/scripts/analyze-diaspora/rewrite-left-outer-joins.sh > conditioned-queries.json
    )

    (cd $HOME/dse/concolic_driver; sbt -mem "$MEM_MB" "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ConvertToSqlViews $config_file $paths_dir")

    END=$(date +%s.%N)
    DIFF=$(echo "$END - $START" | bc)
    echo "$DIFF" > "$paths_dir/post-processing-time-sec.txt"
done
