#!/usr/bin/env python3
import argparse
import json
import os
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('app', type=str, help='The name of the application to run')
    parser.add_argument('test_name', type=str, help='The name of the test to run')
    parser.add_argument('invocation_path', type=str, help='The path to the invocation file')
    parser.add_argument('output_path', type=str, help='Where to output the transcript')
    args = parser.parse_args()

    with open(args.invocation_path, 'r') as f:
        invocation = json.load(f)

    env_vars = os.environ.copy()
    env_vars['DSE_OUT_PATH'] = args.output_path
    for key, value in invocation['inputVars'].items():
        env_vars[f'DSE_IN_{key}'] = value
    env_vars['DSE_DB_IN'] = '\n'.join(invocation['dbSetupStmts'])

    cmdline = ["bin/rspec_dse", "spec/controllers/invoke_all_controller_spec.rb", "--example", args.test_name]
    subprocess.run(' '.join(cmdline),
                   cwd=f"/home/ubuntu/dse/{args.app}",
                   env=env_vars, check=True, shell=True)


if __name__ == '__main__':
    main()
