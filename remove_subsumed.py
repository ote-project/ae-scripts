#!/usr/bin/env python3
from dataclasses import dataclass
from pathlib import Path
import re
from shutil import copytree
import subprocess
import sys
from tempfile import TemporaryDirectory

from tqdm import tqdm

BLOCKAID_DIR = "/home/ubuntu/dse/blockaid"
BLOCKAID_CMD_LINE = (
    'mvn', 'exec:java', '-Dexec.mainClass="edu.berkeley.cs.netsys.privacy_proxy.cmdline.CheckQuery"',
    '-Dexec.args="jdbc:privacy:thin:/home/ubuntu/dse/diaspora/policy,jdbc:mysql://localhost:3306/diaspora_test?allowPublicKeyRetrieval=true&useSSL=false,diaspora_test diaspora 12345678"',
    '-Dblockaid.enable_caching=false', '-Dblockaid.fast_non_compliance_check=true', '-Dblockaid.solve_timeout_ms=15000'
)


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
                f'-Dblockaid.enable_caching=false -Dblockaid.fast_non_compliance_check=true '
                f'-Dblockaid.solve_timeout_ms=2000')


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
            return False


def compute_num_tables(query: str) -> int:
    match = re.search(r"FROM\s+(.+)\s+WHERE", query, re.IGNORECASE)
    if match is None:
        return 1
    else:
        return len(match.group(1).split(","))


def remove_subsumed(config: Config, sqls: list[str]) -> list[str]:
    sqls = sorted(sqls, key=compute_num_tables, reverse=True)  # Makes a copy.
    i = 0
    with tqdm(total=len(sqls), desc="Removing subsumed queries") as pbar:
        while i < len(sqls):
            if is_query_compliant(config, sqls[:i] + sqls[i + 1:], sqls[i]):
                del sqls[i]  # Query i is redundant -- the information it reveals is already in the other queries.
            else:
                i += 1  # We never consider the same query again -- it won't become redundant later.
            pbar.update(1)
    return sqls


def main() -> None:
    if len(sys.argv) < 5:
        print("Usage: remove_subsumed.py <policy_dir> <jdbc_url> <database> <username> <password>")
        sys.exit(1)

    config = Config(policy_dir=sys.argv[1],  # We will use the dependencies stored here, but not the views.
                    jdbc_url=sys.argv[2], database=sys.argv[3], username=sys.argv[4], password=sys.argv[5])

    all_content = sys.stdin.read()
    sqls = [s.replace("\n", " ").strip()  # Make sure each query is on one line.
            for s in all_content.split(";")]

    for s in remove_subsumed(config, sqls):
        print(s + ";")
        print()


if __name__ == '__main__':
    main()
