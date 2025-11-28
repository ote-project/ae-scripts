#!/usr/bin/env bash
set -ex

# Default memory for sbt (in MB)
MEM_MB=102400

# Parse options: allow optional -m <MB>
while getopts ":m:" opt; do
  case $opt in
    m)
      if ! [[ "$OPTARG" =~ ^[0-9]+$ ]]; then
        echo "Error: -m requires an integer argument (got: $OPTARG)" >&2
        exit 1
      fi
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
analysis_id=$(date +"%Y%m%d-%H%M%S")

cd "$HOME/dse"
(cd examples; git pull --ff-only)

config_file="$HOME/dse/examples/autolab_courses_index.conf"
for p in "$HOME"/dse/logs/autolab-*-"$suffix"; do
    START=$(date +%s.%N)

    analysis_dir="$p/analysis-$analysis_id"
    rm -f "$analysis_dir/original-conditioned-queries.json"
    (cd "$HOME/dse/concolic_driver";
     sbt -mem "$MEM_MB" "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.GenerateConditionedQueries \
                      $config_file $p/annotated-paths --analysis-dir $analysis_dir")

    (cd "$analysis_dir";
        if [ ! -f original-conditioned-queries.json ]; then
            mv conditioned-queries.json original-conditioned-queries.json; 
        fi;
        < original-conditioned-queries.json \
            "$HOME/dse/scripts/analyze-autolab/rewrite-aggs.py" | \
            "$HOME/dse/scripts/analyze-autolab/broaden.sh" > conditioned-queries.json
    )

    (cd "$HOME/dse/concolic_driver";
     sbt -mem "$MEM_MB" "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ConvertToSqlViews \
                      $config_file $analysis_dir" >/dev/null)

    END=$(date +%s.%N)
    DIFF=$(echo "$END - $START" | bc)
    echo "$DIFF" > "$analysis_dir/post-processing-time-sec.txt"

    echo "Postprocessing complete.  Suffix: $suffix.  Analysis ID: $analysis_id." 1>&2
done

