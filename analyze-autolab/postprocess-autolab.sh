#!/usr/bin/env bash
set -ex

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
     sbt -mem 102400 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.GenerateConditionedQueries \
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
     sbt -mem 102400 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ConvertToSqlViews \
                      $config_file $analysis_dir" >/dev/null)

    END=$(date +%s.%N)
    DIFF=$(echo "$END - $START" | bc)
    echo "$DIFF" > "$analysis_dir/post-processing-time-sec.txt"

    echo "Postprocessing complete.  Suffix: $suffix.  Analysis ID: $analysis_id." 1>&2
done

