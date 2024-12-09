#!/usr/bin/env python3
from dataclasses import dataclass
import os
from shutil import copy, copytree
import subprocess
import sys
from tempfile import TemporaryDirectory

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


def main() -> None:
    if len(sys.argv) != 7:
        print(f"Usage: {sys.argv[0]} <app_policy_dir> <jdbc_url> <database> <username> <password> <sql_views_path>")
        sys.exit(1)

    _, policy_dir, jdbc_url, database, username, password, views_path = sys.argv
    # We will use the dependencies stored in policy_dir, but not the views.
    with TemporaryDirectory() as temp_dir:
        copytree(policy_dir, temp_dir, dirs_exist_ok=True)
        copy(views_path, os.path.join(temp_dir, "policies.sql"))

        temp_config = Config(policy_dir=temp_dir, jdbc_url=jdbc_url, database=database,
                             username=username, password=password)
        subprocess.run(temp_config.make_blockaid_cmdline(), cwd=BLOCKAID_DIR, shell=True)


if __name__ == '__main__':
    main()

