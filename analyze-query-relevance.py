#!/usr/bin/env python3
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from timeit import default_timer as timer
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm


@dataclass(frozen=True)
class QueryIssuance:
    query: str
    stacktrace: tuple[str, ...]

MAX_WORKERS = 8

APP_DIR = "/home/ubuntu/dse/diaspora"
CUTOFF_PATTERN = re.compile(r"/home/ubuntu/dse/diaspora/app/controllers/posts_controller\.rb:\d+:in `show'")

PROMPT_TEMPLATE = """
I am a program analysis tool that, given a Ruby on Rails application, performs **symbolic execution** to gather (1) all (parameterized) **SQL queries** that the application may issue, and (2) the conditions under which each SQL query is issued.

During symbolic execution on the application in the current directory, I encountered a SQL query (enclosed in the <query></query> tags) issued at the the stacktrace enclosed in the <stacktrace></stacktrace> tags below.
Normally, I would explore multiple possibilities for this query's result---whether it returns no rows, one row, etc.
But if I know that this query's result has no bearing on **subsequent** SQL-query issuance, then I call this query _irrelevant_ and I can save time by going down only one path. Note that "subsequent SQL queries" may include queries issued outside the current method---e.g., if this query's result affects the method's return value, which affects whether or not the method's caller issues another SQL query, then this query _is_ relevant.

**Question: Is this query relevant---can it affect whether a later SQL query gets issued?**

<query>
{query}
</query>

<stacktrace>
{stacktrace}
</stacktrace>

Inspect the code and **let me know whether this query is relevant**---i.e., affecting whether a later SQL query gets issued. Start your answer with "Yes", "No", or "Unsure", then explain your answer. Answer "Yes" or "No" only if you are certain. I don't want to accidentally skip over a query that is relevant, so err on the side of caution ("Unsure").
"""


def main():
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
        prompt = PROMPT_TEMPLATE.format(
            query=qi.query,
            stacktrace="\n".join(qi.stacktrace)
        )
        
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt') as temp_file:
            last_message_path = Path(temp_file.name)
        
        try:
            start_ts = timer()
            proc = subprocess.Popen(
                ["codex", "exec", "--sandbox", "read-only", "--output-last-message", str(last_message_path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=APP_DIR
            )
            stdout, stderr = proc.communicate(prompt)
            exit_code = proc.returncode
            dur_s = timer() - start_ts

            last_message = last_message_path.read_text()
            
            # Set verdict based on the start of last_message
            if last_message.startswith("Yes"):
                verdict = "Yes"
            elif last_message.startswith("No"):
                verdict = "No"
            elif last_message.startswith("Unsure"):
                verdict = "Unsure"
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
    max_workers = min(MAX_WORKERS, len(query_issuances))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_qi = {executor.submit(process_query_issuance, qi): qi for qi in query_issuances}
        
        # Process completed tasks with progress bar
        for future in tqdm(as_completed(future_to_qi), total=len(query_issuances)):
            try:
                result = future.result()
                json.dump(result, sys.stdout)
                sys.stdout.write("\n")
            except Exception as e:
                qi = future_to_qi[future]
                print(f"Error processing query: {e}", file=sys.stderr)
                # Output error result
                json.dump({
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
                }, sys.stdout)
                sys.stdout.write("\n")


if __name__ == "__main__":
    main()
