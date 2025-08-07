#!/usr/bin/env python3
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from timeit import default_timer as timer
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm


@dataclass(frozen=True)
class QueryIssuance:
    query: str
    stacktrace: tuple[str, ...]

MAX_WORKERS = 8
TIMEOUT_SEC = 180
MAX_RETRIES = 3
RETRY_BACKOFF = 5

APP_DIR = "/home/ubuntu/dse/diaspora"
CUTOFF_PATTERN = re.compile(r"/home/ubuntu/dse/diaspora/app/controllers/posts_controller\.rb:\d+:in `show'")

PROMPT_TEMPLATE = """
<instructions>
You are being called by a program analysis tool called Ote that, given a Ruby on Rails application, performs symbolic execution to gather all (parameterized) **SQL queries** that the application may issue, and the conditions under which each SQL query is issued.

During symbolic execution on the application in the current directory, Ote encountered a SQL query (in the <query> tags below) issued at the the stacktrace enclosed in the <stacktrace> tags below.
Normally, Ote would explore multiple possibilities for this query's result---whether it returns no rows, one row, etc.
But if this query's result has no bearing on **subsequent** SQL-query issuance, then this query is called _irrelevant_ and Ote can save time by going down only one path.
Note that "subsequent SQL queries" may include queries issued outside the current method---e.g., if this query's result affects the method's return value, which affects whether or not the method's caller issues another SQL query, then this query _is_ relevant.

**Answer this question**: Is this query is relevant---can it possibly affect whether a subsequent SQL query gets issued?
</instructions>

<query>
{query}
</query>

<stacktrace>
{stacktrace}
</stacktrace>

<requirements>
- You are asked **not** whether _this query_ is issued conditionally, but whether this query's result may affect the issuance of a **subsequent SQL query**.
- "Subsequent SQL queries" includes not only explicit manual queries from application code, but also any hidden ORM-driven calls (association preloads, default scopes, serializers, etc.).
  - Even if the application code doesn't explicitly branch on the result, note that returning zero rows can suppress association-loading queries---this zero-vs-nonzero outcome is itself a branching point you must consider.
  - If an ORM call could trigger additional SQL depending on whether the result set is empty or not, mark it as RELEVANT.
  - To reason about this, you should identify _the exact expression_ in the code that triggered this SQL query, and then inspect any subsequent uses of that expression.
- You MUST err on the side of caution. The worst-case scenario is marking a query as IRRELEVANT when it is actually RELEVANT---this could cause Ote to miss important execution paths.
</requirements>

<output-format>
- **Answer whether this query is relevant**---i.e., affecting whether a subsequent SQL query gets issued---or answer that you are unsure.
- You MUST start your answer with the string `RELEVANT`, `IRRELEVANT`, or `UNSURE`; this part will be parsed by a program, so you must not change the format.
- Then, unless you answered `UNSURE`, you MUST rigorously  explain your verdict:
  - You will be graded on your explanation. You MUST ensure that a human developer, by reading your explanation and cross-referencing the codebase, will be convinced of your verdict.
  - Where applicable, you MUST identify the **precise expression $E$** in the code that represents the result of this SQL query. DO NOT just reproduce an entire line of code; focus on the relevant expression.
    - If there is no explicit expression for the query's result at query time (e.g., because the query is eager-loading an association), you MUST identify the **precise code snippet** that triggered this SQL query.
  - If your verdict is `RELEVANT`, you MUST be specific about **what data** is subsequently fetched depending on this query's result and **how**.
  - If your verdict is `IRRELEVANT` and an expression $E$ exists, you MUST carefully go through subsequent uses of $E$ in the code and explain why they do not trigger SQL queries depending on the current query's outcome.
</output-format>
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
    max_workers = min(MAX_WORKERS, len(query_issuances))
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
