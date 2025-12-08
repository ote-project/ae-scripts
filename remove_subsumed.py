#!/usr/bin/env python3
import argparse
from dataclasses import dataclass
from pathlib import Path
import re
from shutil import copytree
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Optional

from tqdm import tqdm

BLOCKAID_DIR = "/home/ubuntu/dse/blockaid"

@dataclass
class Config:
    policy_dir: str
    jdbc_url: str
    database: str
    username: str
    password: str

    def make_blockaid_cmdline(self) -> str:
        return (f'mvn exec:java '
                f'-Dexec.mainClass="edu.berkeley.cs.netsys.privacy_proxy.cmdline.CheckQuery" '
                f'-Dexec.args="jdbc:privacy:thin:{self.policy_dir},{self.jdbc_url},{self.database} {self.username} {self.password}" '
                '-Dblockaid.enable_caching=false '
                # '-Dblockaid.fast_non_compliance_check=true '
                '-Dblockaid.solve_timeout_ms=5000')


def is_query_compliant(config: Config, views: list[str], query: str) -> bool:
    with TemporaryDirectory() as temp_dir:
        copytree(config.policy_dir, temp_dir, dirs_exist_ok=True)
        with Path(temp_dir, "policies.sql").open("w") as f:
            for sql in views:
                print(sql + ";", file=f)

        temp_config = Config(policy_dir=temp_dir, jdbc_url=config.jdbc_url, database=config.database,
                             username=config.username, password=config.password)
        result = subprocess.run(temp_config.make_blockaid_cmdline(), cwd=BLOCKAID_DIR, shell=True, input=query + "\n",
                                capture_output=True, text=True)

        if result.returncode != 0:
            print(f"*** Blockaid failed with return code {result.returncode}.", file=sys.stderr)
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return False

        if "Query is compliant" in result.stdout:
            return True
        elif "Query is NOT compliant" in result.stdout:
            return False
        else:
            print("*** Unexpected output from Blockaid:", file=sys.stderr)
            print(result.stdout, file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            sys.exit(1)


def compute_num_tables(query: str) -> int:
    match = re.search(r"FROM\s+(.+)\s+WHERE", query, re.IGNORECASE)
    if match is None:
        return 1
    else:
        return len(match.group(1).split(","))


def compute_query_complexity(query: str) -> tuple[int, int]:
    return (compute_num_tables(query), len(query))


class BlockaidProcessManager:
    """Manages a persistent Blockaid process that can handle multiple queries."""

    def __init__(self, config: Config):
        self.config = config
        self.current_kept_queries: tuple[str, ...] = ()
        self.temp_dir: Optional[TemporaryDirectory] = None
        self.process: Optional[subprocess.Popen[str]] = None

    def _start_process(self, kept_queries: list[str]) -> None:
        """Start a new Blockaid process with the given kept_queries."""
        # Clean up old process if exists
        if self.process is not None:
            self._terminate_process()

        # Clean up old temp directory if exists
        if self.temp_dir is not None:
            self.temp_dir.cleanup()

        # Create new temp directory
        self.temp_dir = TemporaryDirectory()
        temp_dir_path = self.temp_dir.name

        # Copy policy directory
        copytree(self.config.policy_dir, temp_dir_path, dirs_exist_ok=True)

        # Write policies.sql
        with Path(temp_dir_path, "policies.sql").open("w") as f:
            for sql in kept_queries:
                print(sql + ";", file=f)

        # Create temp config
        temp_config = Config(
            policy_dir=temp_dir_path,
            jdbc_url=self.config.jdbc_url,
            database=self.config.database,
            username=self.config.username,
            password=self.config.password
        )

        # Launch new process
        self.process = subprocess.Popen(
            temp_config.make_blockaid_cmdline(),
            cwd=BLOCKAID_DIR,
            shell=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0  # Unbuffered for immediate I/O
        )

        self.current_kept_queries = tuple(kept_queries)

    def _terminate_process(self) -> None:
        """Terminate the current process by sending blank line and waiting."""
        if self.process is None:
            return

        try:
            self.process.stdin.write("\n")
            self.process.stdin.flush()
            self.process.wait()
        except (BrokenPipeError, ValueError):
            # Process may have already exited
            pass
        finally:
            self.process = None

    def check_compliance(self, query: str, kept_queries: list[str]) -> bool:
        """Check if query is compliant against kept_queries. Returns True if compliant (redundant)."""
        if not kept_queries:
            # TODO(zhangwen): We should return true if the query is a constant query, but Blockaid doesn't handle empty kept_queries?
            return False

        kept_queries_tuple = tuple(kept_queries)

        # If kept_queries changed, restart process
        if kept_queries_tuple != self.current_kept_queries:
            self._start_process(kept_queries)

        # Ensure process is running
        if self.process is None:
            self._start_process(kept_queries)

        # Send query to process
        try:
            self.process.stdin.write(query + "\n")
            self.process.stdin.flush()

            # Read output - Blockaid outputs one result per query
            # We need to read until we get the compliance result
            output_lines = []
            found_result = False

            # Read lines until we find the compliance result
            # Blockaid should output the result relatively quickly
            while not found_result:
                line = self.process.stdout.readline()
                if not line:
                    # Check if process died
                    if self.process.poll() is not None:
                        break
                    # Otherwise, might be waiting for more input or buffering
                    continue

                output_lines.append(line)
                # Check if we have the result
                if "Query is compliant" in line or "Query is NOT compliant" in line:
                    found_result = True
                    break

            output = "".join(output_lines)

            # Check if process died unexpectedly
            if self.process.poll() is not None:
                if self.process.returncode != 0:
                    stderr_output = ""
                    if self.process.stderr:
                        try:
                            stderr_output = self.process.stderr.read()
                        except:
                            pass
                    print(f"*** Blockaid failed with return code {self.process.returncode}.", file=sys.stderr)
                    print(output, file=sys.stderr)
                    if stderr_output:
                        print(stderr_output, file=sys.stderr)
                    self.process = None
                    return False
                # Process exited normally but we didn't get a result
                if not found_result:
                    print("*** Blockaid process exited unexpectedly.", file=sys.stderr)
                    print(output, file=sys.stderr)
                    self.process = None
                    return False

            # Parse output
            if "Query is compliant" in output:
                return True
            elif "Query is NOT compliant" in output:
                return False
            else:
                # Didn't find expected output
                print("*** Unexpected output from Blockaid:", file=sys.stderr)
                print(output, file=sys.stderr)
                if self.process.stderr:
                    try:
                        stderr_output = self.process.stderr.read()
                        if stderr_output:
                            print(stderr_output, file=sys.stderr)
                    except:
                        pass
                sys.exit(1)

        except (BrokenPipeError, OSError) as e:
            print(f"*** Blockaid process error: {e}", file=sys.stderr)
            self.process = None
            return False

    def cleanup(self) -> None:
        """Clean up process and temp directory."""
        self._terminate_process()
        if self.temp_dir is not None:
            self.temp_dir.cleanup()
            self.temp_dir = None


def remove_subsumed_and_print(config: Config, sqls: list[str]) -> None:
    sqls = sorted(sqls, key=compute_query_complexity, reverse=True)  # Most complex queries first.
    i = 0
    with tqdm(total=len(sqls), desc="Removing subsumed queries") as pbar:
        while i < len(sqls):
            curr = sqls[i]
            if is_query_compliant(config, sqls[:i] + sqls[i + 1:], curr):
                print(f"Removed redundant:\t{curr}", file=sys.stderr)
                del sqls[i]  # Query i is redundant -- the information it reveals is already in the other queries.
            else:
                print(curr + ";")
                print(flush=True)
                i += 1  # We never consider the same query again -- it won't become redundant later.
            pbar.update(1)


def remove_subsumed_optimized(config: Config, sqls: list[str]) -> None:
    """Optimized version that reuses Blockaid process when kept_queries doesn't change."""
    sqls = sorted(sqls, key=compute_query_complexity)  # Least complex queries first.
    kept_queries: list[str] = []
    process_manager = BlockaidProcessManager(config)

    try:
        for query in tqdm(sqls, desc="Removing subsumed queries (optimized)"):
            # Check if query is compliant against kept_queries
            if process_manager.check_compliance(query, kept_queries):
                # Query is redundant
                print(f"Removed redundant:\t{query}", file=sys.stderr)
            else:
                # Query is needed, add to kept_queries and print
                kept_queries.append(query)
                print(query + ";")
                print(flush=True)
    finally:
        process_manager.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove subsumed SQL queries")
    parser.add_argument("policy_dir", help="Policy directory")
    parser.add_argument("jdbc_url", help="JDBC URL")
    parser.add_argument("database", help="Database name")
    parser.add_argument("username", help="Username")
    parser.add_argument("password", help="Password")
    parser.add_argument("--optimized", action="store_true", help="Use optimized mode with process reuse")

    args = parser.parse_args()

    config = Config(
        policy_dir=args.policy_dir,  # We will use the dependencies stored in policy_dir, but not the views.
        jdbc_url=args.jdbc_url,
        database=args.database,
        username=args.username,
        password=args.password
    )

    sqls = sys.stdin.read().split(";")
    sqls = [s.replace("\n", " ").strip() for s in sqls]  # Make sure each query is on one line.
    sqls = [s for s in sqls if s]  # Remove empty strings.

    if args.optimized or len(sqls) >= 500:
        remove_subsumed_optimized(config, sqls)
    else:
        remove_subsumed_and_print(config, sqls)


if __name__ == '__main__':
    main()

