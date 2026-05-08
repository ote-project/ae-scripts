# Ote Scripts

Entrypoint for the artifact accompanying our OSDI '26 paper, containing orchestration, post-processing, and analysis scripts for Ote.
These scripts are meant to be used in the virtual machine for our artifact.

## Repository layout

Here are the most important files in this repository:

```
scripts/
├── run-ae.sh                   # Experiment runner (main entry point)
├── analyze-autolab/            # Post-processing pipeline for Autolab
├── analyze-diaspora/           # Post-processing pipeline for Diaspora
├── analyze-theodinproject/     # Post-processing pipeline for The Odin Project
├── dashboard/                  # NiceGUI web dashboard for browsing results
├── constraints_extactor/       # Ruby tools for extracting DB constraints from Rails apps
├── enforce.py                  # Enforce extracted policies via Blockaid
├── remove_subsumed.py          # Remove redundant/subsumed policy views
├── make_table.py               # Generate LaTeX evaluation tables
└── compare_verdicts.py         # Compare query relevance verdicts across runs
```
