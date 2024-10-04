#!/usr/bin/env python3
"""
Launches a DSE container for an application.  The tunnel path is specified in the DSE_TUNNEL_PATH environment variable.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app_name', help='The name of the application to launch')
    parser.add_argument('--extra_env', nargs='*',
                        help='Extra environment variables to pass to the docker run command (e.g. VAR=value)')
    args, extra_args = parser.parse_known_args()

    tunnel_path_str = os.environ.get('DSE_TUNNEL_PATH')
    if tunnel_path_str is None:
        print('DSE_TUNNEL_PATH environment variable is not set', file=sys.stderr)
        sys.exit(1)

    tunnel_path = Path(tunnel_path_str)
    tunnel_path_parent = tunnel_path.resolve().parent
    if not tunnel_path_parent.exists():
        print(f'Tunnel parent directory {tunnel_path_parent} does not exist', file=sys.stderr)
        sys.exit(1)
    tunnel_path_basename = tunnel_path.name

    extra_env_args = []
    for env_var in args.extra_env:
        extra_env_args.extend(['--env', env_var])

    command = [
        'docker', 'run',
        '-v', f'{tunnel_path_parent}:/var/run/dse',
        '--env', f'DSE_TUNNEL_PATH=/var/run/dse/{tunnel_path_basename}',
        *extra_env_args,
        '--tmpfs', '/tmpfs',
        '--rm',
        f'{args.app_name}-dse',
        *extra_args,
    ]
    print('Executing: ' + ' '.join(command), file=sys.stderr)
    subprocess.run(command)


if __name__ == '__main__':
    main()
