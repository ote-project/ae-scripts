#!/usr/bin/env python3
"""
Creates the statistics and performance table in LaTeX.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
import re
import statistics
import sys
import textwrap
from typing import TextIO

from tqdm import tqdm
import yaml

# google-cloud-storage is optional — only required for gs:// data sources.
try:
    from google.cloud import storage
    from google.cloud.storage.bucket import Bucket
    GCLOUD_AVAILABLE = True
except ImportError:
    storage = None  # type: ignore[assignment]
    Bucket = None  # type: ignore[assignment]
    GCLOUD_AVAILABLE = False


@dataclass(frozen=True)
class Application:
    display_name: str  # Display name in the table; may use LaTeX.
    identifier: str  # How the application is identified in result paths.
    run_folder_fmt: str  # e.g., "{app}-{controller}-{action}-{suffix}"
    handlers: tuple["Handler", ...]  # Handlers in the application.
    policy_folder_fmt: str = "{app}-{suffix}-policy"


@dataclass(frozen=True)
class Handler:
    controller: str
    action: str
    custom_display_name: str = None  # Optional; may use LaTeX.

    @property
    def display_name(self) -> str:
        if self.custom_display_name is not None:
            return self.custom_display_name
        return f"{self.controller.capitalize()}\\#{self.action}"


class DataSource(ABC):
    """Abstract base class for data sources (bucket or local directory)."""
    
    @abstractmethod
    def read_file(self, path: str) -> str:
        """Read a file from the data source and return its contents as a string."""
        pass
    
    @abstractmethod
    def list_files(self, prefix: str) -> list[str]:
        """List files under the given prefix (relative path)."""
        pass
    
    @abstractmethod
    def list_dirs(self, prefix: str = "") -> list[str]:
        """List directories (folders) that start with the given prefix. Returns relative paths."""
        pass
    
    @abstractmethod
    def path_exists(self, path: str) -> bool:
        """Check if a path (file or directory) exists."""
        pass


class BucketDataSource(DataSource):
    """Data source that reads from a Google Cloud Storage bucket."""
    
    def __init__(self, bucket: Bucket):
        self.bucket = bucket
    
    def read_file(self, path: str) -> str:
        blob = self.bucket.get_blob(path)
        if blob is None:
            raise FileNotFoundError(f"Blob not found: {path}")
        return blob.download_as_text()
    
    def list_files(self, prefix: str) -> list[str]:
        return [blob.name for blob in self.bucket.list_blobs(prefix=prefix)]
    
    def list_dirs(self, prefix: str = "") -> list[str]:
        """List directories (folders) that start with the given prefix."""
        # In GCS, use delimiter="/" to get directory prefixes
        dirs = set()
        iterator = self.bucket.list_blobs(prefix=prefix, delimiter="/")
        
        # Get directory prefixes (these are the "folders")
        for prefix_path in iterator.prefixes:
            dir_name = prefix_path.rstrip("/")
            if dir_name.startswith(prefix):
                dirs.add(dir_name)
        
        return sorted(dirs)
    
    def path_exists(self, path: str) -> bool:
        """Check if a path (file or directory) exists."""
        # Check if it's a file (blob)
        blob = self.bucket.get_blob(path)
        if blob is not None:
            return True
        # Check if it's a directory (prefix)
        iterator = self.bucket.list_blobs(prefix=path, delimiter="/", max_results=1)
        if list(iterator.prefixes):
            return True
        # Also check if any blob starts with this path (for files in the directory)
        iterator = self.bucket.list_blobs(prefix=path, max_results=1)
        if list(iterator):
            return True
        return False


class LocalDataSource(DataSource):
    """Data source that reads from a local directory."""
    
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        if not self.base_dir.is_dir():
            raise ValueError(f"Local directory does not exist: {base_dir}")
    
    def read_file(self, path: str) -> str:
        file_path = self.base_dir / path
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        return file_path.read_text()
    
    def list_files(self, prefix: str) -> list[str]:
        target_dir = self.base_dir / prefix
        if not target_dir.exists():
            return []
        if target_dir.is_file():
            return [str(target_dir.relative_to(self.base_dir))]
        return [
            str(p.relative_to(self.base_dir))
            for p in target_dir.rglob("*")
            if p.is_file()
        ]
    
    def list_dirs(self, prefix: str = "") -> list[str]:
        """List directories (folders) that start with the given prefix."""
        dirs = []
        # List all top-level directories in the base directory
        for item in self.base_dir.iterdir():
            if item.is_dir():
                rel_path = str(item.relative_to(self.base_dir))
                if prefix == "" or rel_path.startswith(prefix):
                    dirs.append(rel_path)
        
        return sorted(dirs)
    
    def path_exists(self, path: str) -> bool:
        """Check if a path (file or directory) exists."""
        file_path = self.base_dir / path
        return file_path.exists()


APPS: tuple[Application, ...] = (
    Application(
        display_name=r"\diaspora",
        identifier="diaspora",
        run_folder_fmt="{app}-{controller}-{action}-2r-{suffix}",
        handlers=(
            Handler(controller="people", action="stream"),
            Handler(controller="posts", action="show"),
            Handler(controller="people", action="show"),
            Handler(controller="notifications", action="index"),
            Handler(controller="conversations", action="index"),
            Handler(controller="comments", action="index"),
        ),
    ),
    Application(
        display_name="Autolab",
        identifier="autolab",
        run_folder_fmt="{app}-{controller}-{action}-2r-{suffix}",
        handlers=(
            Handler(controller="assessments", action="show"),
            Handler(controller="assessments", action="viewGradesheet", custom_display_name="Assessments\\#gradesheet"),
            Handler(controller="assessments", action="index"),
            Handler(controller="submissions", action="download"),
            Handler(controller="courses", action="index"),
            Handler(controller="metrics", action="get-num-pending-instances", custom_display_name="Metrics\\#getNumPending"),
        ),
    ),
    Application(
        display_name="The Odin Project",
        identifier="theodinproject",
        run_folder_fmt="{app}-{controller}-{action}-2r-{suffix}",
        handlers=(
            Handler(controller="lessons", action="show"),
            Handler(controller="courses", action="show"),
            Handler(controller="project-submissions", action="index",
                    custom_display_name="ProjectSubmissions\\#index"),
            Handler(controller="users", action="show"),
            Handler(controller="paths", action="index"),
            Handler(controller="sitemap", action="index"),
        ),
    )
)


def find_folder_by_prefix(data_source: DataSource, folder_prefix: str) -> str:
    """
    Find a folder whose name starts with the given prefix.
    Raises an error if multiple matches are found or if no matches are found.
    """
    all_dirs = data_source.list_dirs()
    matching_dirs = [d for d in all_dirs if d.startswith(folder_prefix)]

    if len(matching_dirs) == 0:
        raise FileNotFoundError(f"No folder found starting with '{folder_prefix}'")
    if len(matching_dirs) > 1:
        raise ValueError(f"Multiple folders found starting with '{folder_prefix}': {matching_dirs}")

    return matching_dirs[0]


def get_analysis_path(data_source: DataSource, folder: str, analysis_id: str | None) -> str:
    """
    Compute the analysis path for a folder.
    If analysis_id is specified and {folder}/analysis-{analysis_id} exists, use that.
    Otherwise, use {folder}/annotated-paths.
    """
    if analysis_id is not None:
        analysis_dir = f"{folder}/analysis-{analysis_id}"
        if data_source.path_exists(analysis_dir):
            return analysis_dir
    return f"{folder}/annotated-paths"


class ValidateSuffixes(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None) -> None:
        try:
            d = yaml.safe_load(values)
        except yaml.YAMLError:
            # Not valid YAML - treat as a single suffix for all applications
            if not re.match(r'^[a-zA-Z0-9-]+$', values):
                raise argparse.ArgumentError(self, "If not a YAML dictionary, suffix must consist of alphanumeric characters and dashes only.")
            d = {app.identifier: values for app in APPS}
        else:
            if not isinstance(d, dict):
                # Valid YAML but not a dictionary - treat as a single suffix for all applications
                if not re.match(r'^[a-zA-Z0-9-]+$', values):
                    raise argparse.ArgumentError(self, "If not a YAML dictionary, suffix must consist of alphanumeric characters and dashes only.")
                d = {app.identifier: values for app in APPS}
            else:
                # Valid YAML dictionary - validate all apps are present
                for app in APPS:
                    if app.identifier not in d:
                        raise argparse.ArgumentError(self, f"Missing suffix for application '{app.identifier}'.")
        setattr(namespace, self.dest, d)


class ValidateOutputDirectory(argparse.Action):
    def __call__(self, parser, namespace, path, option_string=None) -> None:
        assert isinstance(path, Path)
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
        setattr(namespace, self.dest, path)


def print_table_header(f: TextIO, command_line: str) -> None:
    print(f"% Generated by {command_line}", file=f)
    print(textwrap.dedent(r"""\begin{NiceTabular}{
        l % handler
        r % #paths
        r % #CQs start
        @{\hskip 0.5\tabcolsep}c@{\hskip 0.5\tabcolsep}
        @{\hskip 0pt}r % #CQs end
        r % #views
        l % #final views
        r % Exploration time
        r % CQ simplification time
        r % View pruning time (per handler)
        r % View pruning time (together)
        }
        \toprule
        & \Block{1-6}{\textbf{Statistics}} & & & & & & \Block{1-4}{\textbf{Running Time}} & & & \\
        \cmidrule(lr){2-7} \cmidrule(lr){8-11}
        \textbf{Handler} & \#Paths & \Block{1-3}{\#Cond.~Queries} & & & \Block{1-2}{\#SQL Views} & & Explore & Simplify & Prune & Final Prune \\
        """).strip(), file=f)


def read_float(data_source: DataSource, path: str) -> float:
    return float(data_source.read_file(path))


def count_views(data_source: DataSource, path: str) -> int:
    views = data_source.read_file(path)
    return sum(1 for line in views.splitlines() if line.startswith("SELECT"))


def format_time_s(seconds: float) -> str:
    if seconds < 1:
        raise ValueError("Not supported: <1 second.")
    if seconds < 60:
        return r"\SI{%.0f}{\second}" % seconds
    if seconds < 3600:
        return r"\SI{%.0f}{\minute}" % (seconds / 60)
    return r"\SI{%.1f}{\hour}" % (seconds / 3600)


def get_oracle_files(data_source: DataSource, run_folder: str) -> tuple[list[str], str | None]:
    """
    Get oracle log files for a run folder.  Prefers 'match' (mock codex) logs
    when present; falls back to 'codex' logs.  Returns (files, kind) where
    kind is 'match', 'codex', or None.
    """
    logs_prefix = f"{run_folder}/oracle-logs/"
    all_files = data_source.list_files(logs_prefix)
    for kind in ("match", "codex"):
        files = [p for p in all_files if Path(p).name.startswith(kind) and p.endswith(".jsonl")]
        if files:
            return files, kind
    return [], None


def summarize_oracle_logs(data_source: DataSource, run_folder: str) -> dict | None:
    """Summarize oracle logs for a run folder. Returns None if no logs are found."""
    oracle_files, kind = get_oracle_files(data_source, run_folder)
    if not oracle_files:
        return None

    relevant = 0
    irrelevant = 0
    durations: list[float] = []

    for path in oracle_files:
        content = data_source.read_file(path)
        for line in content.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            verdict = record["verdict"]
            if verdict == "RELEVANT":
                relevant += 1
            elif verdict == "IRRELEVANT":
                irrelevant += 1
            else:
                raise ValueError(f"Unexpected verdict '{verdict}' in {path}")
            # match (mock) oracle logs have no dur_s.
            if "dur_s" in record:
                durations.append(float(record["dur_s"]))

    if relevant + irrelevant == 0:
        return None

    timeout_count = 0
    explore_log_path = f"{run_folder}/logs/explore-executions.log"
    try:
        explore_log = data_source.read_file(explore_log_path)
        timeout_count = sum(
            1 for line in explore_log.splitlines() if "Codex CLI failed" in line
        )
    except FileNotFoundError:
        timeout_count = 0

    total = relevant + irrelevant + timeout_count
    if durations:
        mean_dur = statistics.mean(durations)
        std_dur = statistics.pstdev(durations) if len(durations) > 1 else 0.0
    else:
        mean_dur = None
        std_dur = None

    return {
        "kind": kind,
        "relevant": relevant,
        "irrelevant": irrelevant,
        "timeout": timeout_count,
        "total": total,
        "mean_dur": mean_dur,
        "std_dur": std_dur,
    }


def print_oracle_table_header(f: TextIO, command_line: str, omit_timeout: bool = False) -> None:
    print(f"% Generated by {command_line}", file=f)
    if omit_timeout:
        print(textwrap.dedent(r"""\begin{tabular}{
        l % handler
        r % relevant
        r % irrelevant
        r % duration
        }
        \toprule
        & \multicolumn{2}{c}{\textbf{Counts}} & \textbf{Duration (min)}\durationmark \\
        \cmidrule(lr){2-3}
        \textbf{Handler} & Rel. & Irrel. & (mean $\pm$ std) \\
        """).strip(), file=f)
    else:
        print(textwrap.dedent(r"""\begin{tabular}{
        l % handler
        r % relevant
        r % irrelevant
        r % timeout
        r % duration
        }
        \toprule
        & \multicolumn{3}{c}{\textbf{Counts}} & \textbf{Duration (min)}\durationmark \\
        \cmidrule(lr){2-4}
        \textbf{Handler} & Rel. & Irrel. & Timeout & (mean $\pm$ std) \\
        """).strip(), file=f)


def format_duration_with_std(mean_seconds: float | None, std_seconds: float | None) -> str:
    """Format duration with standard deviation for the oracle table in minutes: "12.3 ± 0.8"."""
    if mean_seconds is None:
        return "---"
    mean_min = mean_seconds / 60
    std_min = std_seconds / 60
    return r"$\num{%.1f} \pm \num{%.1f}$" % (mean_min, std_min)


def print_oracle_app(f: TextIO, app: Application, data_source: DataSource, suffix: str,
                     omit_timeout: bool = False) -> tuple[bool, set[str]]:
    """
    Print oracle statistics for an application.
    Returns (rows_printed, kinds_seen) where kinds_seen is the set of oracle
    kinds ('match' / 'codex') encountered across this app's handlers.
    """
    rows_printed = False
    printed_header = False
    kinds_seen: set[str] = set()
    for handler in tqdm(app.handlers, desc=f"Oracle logs of {app.identifier}"):
        folder_prefix = app.run_folder_fmt.format(app=app.identifier, controller=handler.controller,
        action=handler.action, suffix=suffix)
        folder = find_folder_by_prefix(data_source, folder_prefix)
        stats = summarize_oracle_logs(data_source, folder)
        if stats is None:
            continue
        kinds_seen.add(stats["kind"])

        if not printed_header:
            if omit_timeout:
                print(r"\midrule", file=f)
                print(r"\textbf{%s} & & & \\" % app.display_name, file=f)
            else:
                print(r"\midrule", file=f)
                print(r"\textbf{%s} & & & & \\" % app.display_name, file=f)
            printed_header = True

        rows_printed = True
        duration_str = format_duration_with_std(stats["mean_dur"], stats["std_dur"])
        if omit_timeout:
            f.write(r"\quad\texttt{%s} & \num{%d} & \num{%d} & %s \\" %
                    (
                        handler.display_name,
                        stats["relevant"],
                        stats["irrelevant"],
                        duration_str,
                    ))
        else:
            f.write(r"\quad\texttt{%s} & \num{%d} & \num{%d} & \num{%d} & %s \\" %
                    (
                        handler.display_name,
                        stats["relevant"],
                        stats["irrelevant"],
                        stats["timeout"],
                        duration_str,
                    ))
        f.write("\n")

    return rows_printed, kinds_seen


def print_oracle_table_footer(f: TextIO) -> None:
    print(r"\bottomrule", file=f)
    print(r"\end{tabular}", file=f)


def print_app(f: TextIO, app: Application, data_source: DataSource, suffix: str, analysis_id: str | None = None) -> int:
    """
    Prints the statistics and performance of an application.
    :return: the number of final views.
    """
    policy_folder_prefix = app.policy_folder_fmt.format(app=app.identifier, suffix=suffix)
    policy_folder = find_folder_by_prefix(data_source, policy_folder_prefix)
    final_prune_dur_s = read_float(data_source, f"{policy_folder}/remove-subsumed-time-sec.txt")
    num_final_views = count_views(data_source, f"{policy_folder}/all-minimized.sql")

    print(r"\midrule", file=f)
    print(r"\textbf{%s} & & & & & & & & & & %s \\" % (app.display_name, format_time_s(final_prune_dur_s)), file=f)

    for i, handler in enumerate(tqdm(app.handlers, desc=f"Handlers of {app.identifier}")):
        folder_prefix = app.run_folder_fmt.format(app=app.identifier, controller=handler.controller,
                                                  action=handler.action, suffix=suffix)
        folder = find_folder_by_prefix(data_source, folder_prefix)
        analysis_path = get_analysis_path(data_source, folder, analysis_id)

        runs_csv_path = f"{folder}/metrics/edu.berkeley.cs.netsys.policy_extraction.cmdline.ExploreExecutionsImpl.runs.csv"
        runs_content = data_source.read_file(runs_csv_path)
        runs = list(csv.DictReader(runs_content.splitlines()))
        explore_timestamps_s = [int(row["t"]) for row in runs]
        explore_dur_s = max(explore_timestamps_s) - min(explore_timestamps_s)
        num_paths = max(int(row["count"]) for row in runs)

        gen_cqs_log: str = data_source.read_file(f"{analysis_path}/generate-cqs.log")
        num_cqs_start = int(re.search(r"Converted to (\d+) conditioned queries\.", gen_cqs_log).group(1))
        num_cqs_end = int(
            re.search(r"There are (\d+) conditioned queries after removing subsumed\.", gen_cqs_log).group(1)
        )

        num_minimized_views = count_views(data_source, f"{analysis_path}/views-minimized.sql")

        simplify_dur_s = read_float(data_source, f"{analysis_path}/post-processing-time-sec.txt")
        prune_dur_s = read_float(data_source, f"{analysis_path}/remove-subsumed-time-sec.txt")

        oracle_files, _ = get_oracle_files(data_source, folder)
        prune_suffix = r" \prune{}" if oracle_files else ""
        f.write(r"\quad\texttt{%s}%s & \num{%d} & \num{%d} & $\to$ & \num{%d} & \num{%d} &" %
                (handler.display_name, prune_suffix, num_paths, num_cqs_start, num_cqs_end, num_minimized_views))
        if i == 0:
            f.write(r"\Block{%d-1}{$\to$ \num{%d}}" % (len(app.handlers), num_final_views))
        print(r"& %s & %s & %s & \\" %
              (format_time_s(explore_dur_s), format_time_s(simplify_dur_s), format_time_s(prune_dur_s)), file=f)

    return num_final_views


def print_table_footer(f: TextIO) -> None:
    print(r"\bottomrule", file=f)
    # Draw right curly braces.
    curr_row = 3
    for app in APPS:
        num_handlers = len(app.handlers)
        print(r"\CodeAfter\SubMatrix.{%d-6}{%d-6}\}" % (curr_row + 1, curr_row + num_handlers), file=f)
        curr_row += 1 + num_handlers  # Skip the application header row and all handler rows.
    print(r"\end{NiceTabular}", file=f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the statistics and performance table in LaTeX.")
    parser.add_argument("data_source", type=str,
                        help="Data source: either 'gs://<bucket_name>' for Google Cloud storage, or a local directory path.")
    parser.add_argument("suffixes", type=str, action=ValidateSuffixes,
                        help="Suffixes for each application's run. Either a YAML dictionary mapping application "
                             "identifiers to suffixes, or a single alphanumeric string (with dashes) to use for all applications.")
    parser.add_argument("output_dir", type=Path, action=ValidateOutputDirectory,
                        help="Output directory for the LaTeX table and macro definitions.")
    parser.add_argument("--analysis-id", type=str, default=None,
                        help="Optional analysis ID. If specified and {folder}/analysis-{analysis-id} exists, use that instead of annotated-paths.")
    parser.add_argument("--omit-oracle-timeout", action="store_true",
                        help="Omit the Timeout column from the oracle table.")
    args = parser.parse_args()

    # Determine if data_source is a local directory or a bucket name
    if args.data_source.startswith("gs://"):
        if not GCLOUD_AVAILABLE:
            parser.error("gs:// data sources require the google-cloud-storage package, which is not installed.")
        bucket_name = args.data_source[5:]  # Remove "gs://" prefix
        if not bucket_name:
            parser.error("Bucket name cannot be empty after 'gs://' prefix.")
        storage_client = storage.Client()
        bucket = storage_client.get_bucket(bucket_name)
        data_source = BucketDataSource(bucket)
    else:
        # Local directory
        data_source_path = Path(args.data_source)
        data_source = LocalDataSource(data_source_path)

    with (args.output_dir / "main-table.tex").open("w") as f:
        print_table_header(f, command_line=" ".join(sys.argv))

        app2num_final_views: dict[Application, int] = {}
        for app in tqdm(APPS, desc="Applications"):
            suffix = args.suffixes[app.identifier]
            num_final_views = print_app(f, app, data_source, suffix, args.analysis_id)
            app2num_final_views[app] = num_final_views

        print_table_footer(f)

    with (args.output_dir / "relevance-table.tex").open("w") as f:
        print_oracle_table_header(f, command_line=" ".join(sys.argv), omit_timeout=args.omit_oracle_timeout)
        any_rows = False
        oracle_kinds: set[str] = set()
        for app in tqdm(APPS, desc="Oracle applications"):
            suffix = args.suffixes[app.identifier]
            printed, kinds = print_oracle_app(f, app, data_source, suffix, omit_timeout=args.omit_oracle_timeout)
            any_rows = any_rows or printed
            oracle_kinds.update(kinds)
        if not any_rows:
            print(r"% No oracle logs found; table is empty.", file=f)
        print_oracle_table_footer(f)

    with (args.output_dir / "stats-macros.tex").open("w") as f:
        # Define some macros for the final-view counts.
        for app, num_final_views in app2num_final_views.items():
            print(r"\newcommand{\%sFinalViews}{\num{%d}\xspace}" % (app.identifier, num_final_views), file=f)

        # Macro indicating which oracle kind was used for the relevance stats.
        if len(oracle_kinds) == 1:
            kind = next(iter(oracle_kinds))
            print(r"\newcommand{\OracleKind}{%s}" % kind, file=f)
        elif len(oracle_kinds) > 1:
            raise ValueError(f"Mixed oracle kinds across apps: {sorted(oracle_kinds)}")


if __name__ == "__main__":
    main()
