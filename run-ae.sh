#!/usr/bin/env bash
set -euo pipefail

# Hardcoded analysis ID for the AE pipeline.
analysis_id="ae"

###############################################################################
# ──  CLI options  ─────────────────────────────────────────────────────────────
###############################################################################
usage() {
    cat <<'USAGE'
Usage: run-ae.sh (once|full) [--no-pull] [--use-codex] [--output-dir DIR]

Artifact-evaluation runner with predefined configurations.  Runs experiments,
postprocesses, removes subsumed views, and renders LaTeX tables.

Presets:
  once   All applications and all handlers (1 run)
  full   All applications and all handlers, repeated 3 times; takes the
         per-handler median by exploration time

Options:
  --no-pull         Skip the git pull steps before running experiments.
  --use-codex       Use the real codex relevance judge instead of the
                    default mock (match) judge.
  --output-dir DIR  Directory for make_table.py output
                    (default: ~/dse/logs/ae-tables-<base>)
USAGE
}

preset=""
do_pull=true
use_codex=false
output_dir=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        once|full)
            [[ -n "$preset" ]] && { echo "Preset already set to '$preset'." >&2; exit 1; }
            preset="$1"
            shift
            ;;
        --no-pull)
            do_pull=false
            shift
            ;;
        --use-codex)
            use_codex=true
            shift
            ;;
        --output-dir)
            [[ $# -lt 2 ]] && { echo "--output-dir requires an argument." >&2; exit 1; }
            output_dir="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ -z "$preset" ]]; then
    echo "❌  Missing preset (once|full)." >&2
    usage >&2
    exit 1
fi

scripts_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Pull the scripts repo so make_table.py, make_ae_pdf.sh, and the analyze-*
# helpers run the latest code.  Changes to run-ae.sh itself take effect on
# the next invocation, since this script is already in flight.
if [[ "$do_pull" == true ]]; then
    (cd "$scripts_dir" && git pull --ff-only)
fi

source "$scripts_dir/_run-lib.sh"

# Sanity-check that make_table.py can start before kicking off the long
# experiment pipeline.  Triggers all imports.
"$scripts_dir/make_table.py" --help >/dev/null || {
    echo "❌  make_table.py failed to start (Python import error above)." >&2
    echo "    Install missing packages with: pip install -r $scripts_dir/requirements.txt" >&2
    exit 1
}

base=$(date +%Y%m%d-%H%M%S)
[[ -z "$output_dir" ]] && output_dir="$HOME/dse/logs/ae-tables-$base"

memory=81920
logging=inputs-only
extra_opts=""

experiments=(
  "${diaspora_experiments[@]}"
  "${autolab_experiments[@]}"
  "${theodinproject_experiments[@]}"
)

postprocess_for_suffix() {
    local s="$1"
    "$scripts_dir/analyze-diaspora/postprocess-diaspora.sh"             -a "$analysis_id" "$s"
    "$scripts_dir/analyze-autolab/postprocess-autolab.sh"               -a "$analysis_id" "$s"
    "$scripts_dir/analyze-theodinproject/postprocess-theodinproject.sh" -a "$analysis_id" "$s"
}

remove_subsumed_for_suffix() {
    local s="$1"
    "$scripts_dir/analyze-diaspora/remove-subsumed.sh"       "$s" "$analysis_id"
    "$scripts_dir/analyze-autolab/remove-subsumed.sh"        "$s" "$analysis_id"
    "$scripts_dir/analyze-theodinproject/remove-subsumed.sh" "$s" "$analysis_id"
}

###############################################################################
# ──  Oracle symlinks  ─────────────────────────────────────────────────────────
###############################################################################
oracle_kind="match"
$use_codex && oracle_kind="codex"

for app in diaspora autolab; do
    link="$HOME/dse/app-config/${app}_oracle.conf"
    target="${app}_oracle_${oracle_kind}.conf"
    if [[ -e "$link" && ! -L "$link" ]]; then
        echo "❌  $link exists and is not a symlink; refusing to overwrite." >&2
        exit 1
    fi
    rm -f "$link"
    ln -s "$target" "$link"
done

###############################################################################
# ──  Run pipeline  ────────────────────────────────────────────────────────────
###############################################################################
case "$preset" in
    once)
        suffix="$base"
        run_experiments
        postprocess_for_suffix "$suffix"
        remove_subsumed_for_suffix "$suffix"
        ;;
    full)
        for i in 1 2 3; do
            suffix="${base}-r${i}"
            run_experiments
        done

        run_dirs=()
        for i in 1 2 3; do
            for exp in "${experiments[@]}"; do
                run_dirs+=("${exp//_/-}-2r-${base}-r${i}")
            done
        done

        median_suffix="${base}-median"
        "$scripts_dir/osdi26/assemble_runs.py" \
            --make-symlinks \
            --out-suffix "$median_suffix" \
            -- \
            "${run_dirs[@]}"

        postprocess_for_suffix "$median_suffix"
        remove_subsumed_for_suffix "$median_suffix"

        suffix="$median_suffix"
        ;;
esac

###############################################################################
# ──  Render tables  ──────────────────────────────────────────────────────────
###############################################################################
mkdir -p "$output_dir"
"$scripts_dir/make_table.py" "$HOME/dse/logs" "$suffix" "$output_dir" --analysis-id "$analysis_id"
"$scripts_dir/make_ae_pdf.sh" --preset "$preset" "$output_dir"

echo "Done." >&2
