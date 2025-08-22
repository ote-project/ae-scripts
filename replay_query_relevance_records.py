#!/usr/bin/env python3
"""
Replay query relevance records by re-running Codex with either the original or
the current prompt.

Reads a JSONL records file (same format consumed by view_query_relevance_report.py)
and re-runs `codex exec` once per record (no retries), capturing stdout, stderr,
last assistant message, duration, and exit code. Outputs a new JSONL stream to
stdout, one JSON object per line (mirroring analyze_query_relevance.py).

You can choose which prompt to use when replaying:
  - original: Use the per-record "prompt" field (must exist in input).
  - current:  Recompute the prompt using the latest template from the
              sibling repo `concolic_driver` and the record's query/stacktrace
              (same logic as view_query_relevance_report.py).

Usage examples:
  - python replay_query_relevance_records.py path/to/records.jsonl \
      --prompt-source original \
      --timeout 180 \
      --cwd /path/to/app \
      --codex-args --model gpt-4o-mini

  - python replay_query_relevance_records.py path/to/records.jsonl \
      --prompt-source current \
      --timeout 180 \
      --cwd /path/to/app \
      --codex-args --model gpt-4o-mini

Notes:
  - When using --prompt-source original, all records must have a non-empty
    "prompt" field; otherwise, the script exits with an error.
  - When using --prompt-source current, the script pulls the latest prompt
    template from ../concolic_driver and constructs prompts from query and
    stacktrace.
  - Extra args for `codex exec` can be passed after `--codex-args`.
  - A per-invocation subprocess timeout is enforced; no retries are attempted.
"""

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from timeit import default_timer as timer
from typing import Any, Generator, Optional

# Optional tqdm progress bar
try:
    from tqdm.auto import tqdm as _tqdm
except Exception:  # pragma: no cover - optional dependency
    _tqdm = None

from query_prompt import generate_query_relevance_prompt


def read_jsonl(path: Path) -> Generator[dict[str, Any], None, None]:
    """Read JSONL file and yield parsed JSON objects."""
    with path.open("r") as fp:
        for line_num, line in enumerate(fp, 1):
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    logging.error(f"Invalid JSON on line {line_num}: {e}")
                    raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Re-generate query relevance records by re-running Codex with "
            "either original or current prompts"
        )
    )
    parser.add_argument("input_file", help="Path to the input JSONL records file")
    parser.add_argument(
        "--prompt-source",
        choices=["original", "current"],
        default="original",
        help=(
            "Which prompt to use when replaying: 'original' uses the per-record prompt; "
            "'current' regenerates the prompt from the latest template. (default: original)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Timeout in seconds for each codex run (default: 180)",
    )
    parser.add_argument(
        "--cwd",
        type=str,
        default=None,
        help=(
            "Working directory to run codex in (e.g., the target app folder). "
            "If omitted, inherits the current working directory."
        ),
    )
    parser.add_argument(
        "--codex-args",
        nargs=argparse.REMAINDER,
        default=[],
        help=(
            "Additional arguments to pass through to `codex exec`. Use like: "
            "--codex-args --model gpt-4o"
        ),
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable tqdm progress bar (stdout remains JSONL)",
    )
    return parser.parse_args()


def run_codex_once(
    prompt: str, timeout_s: int, cwd: Optional[str], extra_args: list[str]
) -> dict[str, Any]:
    """Execute codex with the given prompt and return execution results."""
    # Capture last assistant message to a temp file and ensure it is removed.
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as temp_file:
        last_message_path = Path(temp_file.name)

    try:
        cmd = [
            "codex",
            "exec",
            "--sandbox",
            "read-only",
            "--output-last-message",
            str(last_message_path),
        ]
        if extra_args:
            cmd.extend(extra_args)

        start_ts = timer()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
        try:
            stdout, stderr = proc.communicate(prompt, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        dur_s = timer() - start_ts
        exit_code = proc.returncode

        try:
            last_message = last_message_path.read_text()
        except (OSError, IOError):
            last_message = ""

        return {
            "stdout": stdout or "",
            "stderr": stderr or "",
            "last_message": last_message,
            "dur_s": dur_s,
            "exit_code": exit_code,
        }
    finally:
        try:
            last_message_path.unlink(missing_ok=True)
        except (OSError, IOError):
            pass


def main() -> None:
    """Main entry point for the script."""
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stderr)]
    )
    
    args = parse_args()
    input_path = Path(args.input_file)
    
    logging.info(f"Processing records from: {input_path}")

    # Load all records first to validate presence of original prompts.
    try:
        records = list(read_jsonl(input_path))
    except FileNotFoundError:
        logging.error(f"File not found: {input_path}")
        sys.exit(2)
    except Exception as e:
        logging.error(f"Error reading {input_path}: {e}")
        sys.exit(2)

    if not records:
        logging.error("No records found in input file")
        sys.exit(2)

    # If using original prompts, ensure they exist.
    if args.prompt_source == "original":
        missing_prompt_indices: list[int] = [i for i, r in enumerate(records) if not r.get("prompt")]
        if missing_prompt_indices:
            logging.error(
                f"Some record(s) lack the original 'prompt' field: {', '.join(map(str, missing_prompt_indices))}"
            )
            sys.exit(1)

    # Process sequentially to preserve input order and stream JSONL to stdout.
    total_records = len(records)
    logging.info(f"Processing {total_records} records with timeout={args.timeout}s")

    use_progress = (not args.no_progress) and (_tqdm is not None)
    if (not args.no_progress) and (_tqdm is None):
        logging.info("tqdm not installed; continuing without progress bar")

    pbar = _tqdm(total=total_records, desc="Replaying", unit="rec", file=sys.stderr, leave=False) if use_progress else None

    for idx, record in enumerate(records, 1):
        if args.prompt_source == "original":
            prompt = record["prompt"]
        else:
            prompt = generate_query_relevance_prompt(record["query"], record["stacktrace"])

        if not use_progress:
            logging.info(f"Processing record {idx}/{total_records}")

        execution_result = run_codex_once(
            prompt=prompt,
            timeout_s=args.timeout,
            cwd=args.cwd,
            extra_args=args.codex_args,
        )

        # Compose output record mirroring analyze_query_relevance.py
        output_record = {
            "query": record["query"],
            "stacktrace": record["stacktrace"],
            "stdout": execution_result["stdout"],
            "stderr": execution_result["stderr"],
            "last_message": execution_result["last_message"],
            "dur_s": execution_result["dur_s"],
            "exit_code": execution_result["exit_code"],
            # Preserve the original prompt that was used for this replay for traceability
            "prompt": prompt,
            "replay_prompt_source": args.prompt_source,
        }
        print(json.dumps(output_record, ensure_ascii=False), flush=True)

        if pbar is not None:
            pbar.update(1)

        if execution_result["exit_code"] != 0:
            logging.warning(f"Record {idx} failed with exit code {execution_result['exit_code']}")

    if pbar is not None:
        pbar.close()
    
    logging.info(f"Completed processing {total_records} records")


if __name__ == "__main__":
    main()
