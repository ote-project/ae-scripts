#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: make_ae_pdf.sh [--preset PRESET] <output_dir>

Wraps main-table.tex, relevance-table.tex, and stats-macros.tex (produced by
make_table.py) into ae.pdf inside <output_dir>.

Options:
  --preset PRESET   Renders a red caption warning when PRESET is 'once'
                    (single run, not 3-run median).
USAGE
}

preset=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --preset)
            [[ $# -lt 2 ]] && { echo "--preset requires an argument" >&2; exit 1; }
            preset="$2"; shift 2 ;;
        --*) echo "Unknown flag: $1" >&2; usage >&2; exit 1 ;;
        *) break ;;
    esac
done

case "$preset" in
    ""|once|full) ;;
    *) echo "Invalid --preset '$preset' (expected: once, full, or unset)." >&2; exit 1 ;;
esac

if [[ $# -ne 1 ]]; then
    usage >&2
    exit 1
fi

output_dir="$1"
[[ ! -d "$output_dir" ]] && { echo "Not a directory: $output_dir" >&2; exit 1; }

for f in main-table.tex relevance-table.tex stats-macros.tex; do
    [[ -f "$output_dir/$f" ]] || {
        echo "Missing $output_dir/$f — run make_table.py first." >&2
        exit 1
    }
done

if ! command -v pdflatex >/dev/null; then
    cat >&2 <<'ERR'
pdflatex not found.  Install with:
  sudo apt install -y texlive-latex-recommended texlive-latex-extra texlive-science
ERR
    exit 1
fi

cat > "$output_dir/ae.tex" <<'EOF'
\documentclass{article}

\usepackage[margin=0.5in]{geometry}
\usepackage{booktabs}
\usepackage{nicematrix}
\usepackage{siunitx}
\usepackage{xspace}
\usepackage{graphicx}
\usepackage{pifont}
\usepackage{xcolor}
\usepackage{float}

% Add a bit of breathing room between table captions and the tables themselves.
\setlength{\belowcaptionskip}{10pt}

% Macros from the paper preamble.
\newcommand{\name}{Xtr\xspace}
\newcommand{\diaspora}{diaspora\xspace}
\newcommand{\prune}{\reflectbox{\ding{36}}}

% Cross-reference placeholder; this minimal doc has no labels.
\providecommand{\Cref}[1]{(\textit{#1})}
\providecommand{\cref}[1]{(\textit{#1})}

\input{stats-macros}
\providecommand{\OracleKind}{}

% \AeMode is the run-ae.sh preset (once / full).  The placeholder __PRESET__
% is replaced by make_ae_pdf.sh.
\newcommand{\AeMode}{__PRESET__}
\newcommand{\matchkind}{match}
\newcommand{\oncemode}{once}

% Dagger marker for the "Duration (min)" column header in the relevance
% table, plus a matching note.  Emits nothing when the judge isn't a mock.
\newcommand{\durationmark}{\ifx\OracleKind\matchkind\textsuperscript{$\dagger$}\fi}
\newcommand{\durationnote}{%
  \ifx\OracleKind\matchkind
    \smallskip
    \begin{flushleft}\textsuperscript{$\dagger$}\,Latencies are injected by the mock relevance judge.\end{flushleft}%
  \fi
}

% Render any applicable warnings as red italic centered lines.  Emits
% nothing when the run is canonical (full + non-mock judge).
\newcommand{\warningblock}{%
  \begingroup
  \color{red}\itshape
  \ifx\AeMode\oncemode
    \begin{center}Warning: this is a single run; the full evaluation reports the median of three runs.\end{center}
  \fi
  \ifx\OracleKind\matchkind
    \begin{center}Warning: relevance verdicts come from a mock relevance judge; the full evaluation uses an LLM-based judge.\end{center}
  \fi
  \endgroup
}

\begin{document}
\pagestyle{empty}
\warningblock

% Match the paper's numbering: the database-constraints table (omitted here)
% is Table 1 in the paper, so start the counter at 1 and let \caption tick
% it up to 2 for the first rendered table.
\setcounter{table}{1}

\begin{table}[H]
\caption{{\bf Statistics and performance.} ``\prune{}'' marks runs that used the relevance judge.
Under \underline{Statistics},
``\#Cond.~Queries'' shows the number of conditioned queries before and after simplification;
``\#SQL Views'' shows the number of views after per- and cross-handler pruning.
Under \underline{Running Time}, ``Simplify'' stands for conditioned-query simplification and view generation,
``Prune'' for per-handler view-pruning, and ``Final Prune'' for cross-handler view-pruning.}\label{tbl:main}
\centering\small
\input{main-table}
\end{table}

\begin{table}[H]
\caption{\textbf{Relevance-judge verdicts and running times.}
``Rel.'' denotes relevant; ``Irrel.'' denotes irrelevant.}\label{tbl:relevance}
\centering\small
\input{relevance-table}
\durationnote
\end{table}

\begin{table}[H]
\caption{\textbf{View count in extracted vs handwritten policies.}}\label{tbl:compare}
\centering\small
\begin{tabular}{lrrr}
\toprule
& \textbf{\diaspora} & \textbf{Autolab} & \textbf{Odin} \\
\midrule
\textbf{Extracted policy} & \diasporaFinalViews & \autolabFinalViews & \theodinprojectFinalViews \\
\textbf{Handwritten policy} & 66 & 37 & --- \\
\bottomrule
\end{tabular}
\end{table}

\end{document}
EOF

# Inject the preset value (may be empty for ad-hoc invocations).  The `|`
# delimiter and the strict --preset validation above ensure no special chars
# in $preset can break the substitution.
sed -i "s|__PRESET__|$preset|" "$output_dir/ae.tex"

cd "$output_dir"
run_pdflatex() {
    if ! pdflatex -halt-on-error -interaction=nonstopmode ae.tex >/dev/null; then
        echo "pdflatex failed; see $output_dir/ae.log" >&2
        exit 1
    fi
}
run_pdflatex
run_pdflatex

echo "Wrote: $output_dir/ae.pdf" >&2
