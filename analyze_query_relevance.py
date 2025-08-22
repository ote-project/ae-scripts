#!/usr/bin/env python3
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from timeit import default_timer as timer

from tqdm import tqdm


@dataclass(frozen=True)
class QueryIssuance:
    query: str
    stacktrace: tuple[str, ...]

TIMEOUT_SEC = 180
MAX_RETRIES = 3
RETRY_BACKOFF = 5

APP_DIR = "/home/ubuntu/dse/diaspora"
CUTOFF_PATTERN = re.compile(r"/home/ubuntu/dse/diaspora/app/controllers/posts_controller\.rb:\d+:in `show'")

from query_prompt import generate_query_relevance_prompt


def positive_int(value: str) -> int:
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--max-workers must be an integer")
    if ivalue <= 0:
        raise argparse.ArgumentTypeError("--max-workers must be a positive integer")
    return ivalue


def main():
    parser = argparse.ArgumentParser(description="Analyze query relevance in parallel")
    parser.add_argument(
        "--max-workers",
        type=positive_int,
        default=8,
        help="Maximum number of parallel workers (positive integer, default: 8)",
    )
    args = parser.parse_args()

    data = json.load(sys.stdin)
    query_issuances = set()
    for item in data:
        query = item["sqlQueryDecl"]["query"]
        stacktrace = item["sqlQueryDecl"]["stacktrace"].split("\n")

        cutoff_index = None
        for idx, line in enumerate(stacktrace):
            if CUTOFF_PATTERN.search(line):
                cutoff_index = idx
                break
        if cutoff_index is not None:
            stacktrace = stacktrace[:cutoff_index + 1]

        query_issuances.add(QueryIssuance(query, tuple(stacktrace)))

    def process_query_issuance(qi):
        prompt = generate_query_relevance_prompt(
            qi.query,
            list(qi.stacktrace),
        )
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as temp_file:
            last_message_path = Path(temp_file.name)
        
        try:
            attempt = 0
            while True:
                attempt += 1
                start_ts = timer()
                try:
                    proc = subprocess.Popen(
                        ["codex", "exec", "--sandbox", "read-only", "--output-last-message", str(last_message_path)],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        cwd=APP_DIR
                    )
                    stdout, stderr = proc.communicate(prompt, timeout=TIMEOUT_SEC)
                    exit_code = proc.returncode
                    dur_s = timer() - start_ts
                    break
                except subprocess.TimeoutExpired:
                    proc.kill()
                    stdout, stderr = proc.communicate()
                    dur_s = timer() - start_ts
                    if attempt >= MAX_RETRIES:
                        raise
                    time.sleep(RETRY_BACKOFF * attempt)

            last_message = last_message_path.read_text()
            
            # Set verdict based on the start of last_message
            if last_message.startswith("RELEVANT"):
                verdict = "RELEVANT"
            elif last_message.startswith("IRRELEVANT"):
                verdict = "IRRELEVANT"
            elif last_message.startswith("UNSURE"):
                verdict = "UNSURE"
            else:
                verdict = None

            tokens_pattern = re.compile(r'tokens used: (\d+)')
            tokens_matches = tokens_pattern.findall(stdout)
            tokens_used = int(tokens_matches[-1]) if tokens_matches else None

            result = {
                "query": qi.query,
                "stacktrace": qi.stacktrace,
                "stdout": stdout,
                "stderr": stderr,
                "last_message": last_message,
                "verdict": verdict,
                "tokens_used": tokens_used,
                "dur_s": dur_s,
                "exit_code": exit_code,
            }
            return result
        finally:
            last_message_path.unlink(missing_ok=True)

    # Use ThreadPoolExecutor to parallelize processing
    max_workers = min(args.max_workers, len(query_issuances))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_qi = {executor.submit(process_query_issuance, qi): qi for qi in query_issuances}
        
        # Process completed tasks with progress bar
        for future in tqdm(as_completed(future_to_qi), total=len(query_issuances)):
            try:
                result = future.result()
                print(json.dumps(result, ensure_ascii=False), flush=True)
            except Exception as e:
                qi = future_to_qi[future]
                print(f"Error processing query: {e}", file=sys.stderr)
                # Output error result
                print(json.dumps({
                    "query": qi.query,
                    "stacktrace": qi.stacktrace,
                    "error": str(e),
                    "stdout": "",
                    "stderr": "",
                    "last_message": "",
                    "verdict": None,
                    "tokens_used": None,
                    "dur_s": 0,
                    "exit_code": -1,
                }), flush=True)


if __name__ == "__main__":
    main()
