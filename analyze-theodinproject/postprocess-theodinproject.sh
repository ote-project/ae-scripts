#!/usr/bin/env bash
set -ex

suffix=${1?param missing - suffix.}
analysis_id=$(date +"%Y%m%d-%H%M%S")

cd "$HOME/dse"
(cd examples; git pull --ff-only)

config_file="$HOME/dse/examples/theodinproject_sitemap_index.conf"
for p in "$HOME"/dse/logs/theodinproject-*"$suffix"; do
    START=$(date +%s.%N)

    paths_dir="$(realpath "$p/annotated-paths")"
    analysis_dir="$(realpath "$p/analysis-$analysis_id")"
    rm -f "$paths_dir/original-conditioned-queries.json"
    (cd "$HOME/dse/concolic_driver";
     sbt -mem 4096 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.GenerateConditionedQueries \
      $config_file $paths_dir --analysis-dir $analysis_dir"
    )

    (cd "$analysis_dir";
     if [ ! -f original-conditioned-queries.json ]; then
         mv conditioned-queries.json original-conditioned-queries.json;
     fi;
     "$HOME/dse/scripts/analyze-theodinproject/rewrite-aggs.py" \
       < original-conditioned-queries.json > conditioned-queries.json
    )

    (cd "$HOME/dse/concolic_driver";
     sbt -mem 4096 "runMain edu.berkeley.cs.netsys.policy_extraction.cmdline.ConvertToSqlViews \
      $config_file $analysis_dir"
    )

    END=$(date +%s.%N)
    DIFF=$(echo "$END - $START" | bc)
    echo "$DIFF" > "$analysis_dir/post-processing-time-sec.txt"

    echo "Postprocessing complete.  Suffix: $suffix, analysis ID: $analysis_id." 1>&2
done
