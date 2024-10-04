#!/usr/bin/env bash
# I am the entry point for an application's Docker container.
# I should be placed in the application's `bin` directory.
set -em

# Move MySQL data to tmpfs, then start MySQL.
if [ ! -d "/tmpfs" ]; then
  echo "Directory /tmpfs does not exist."
  exit 1
fi
mv /var/lib/mysql /tmpfs/mysql
ln -s /tmpfs/mysql /var/lib/mysql

mysqld &
while ! mysqladmin ping --silent; do
  sleep 1
  if ! pgrep -x "mysqld" > /dev/null
  then
    cat /var/log/mysql/*.log
    exit 1
  fi
done

# Now start the application.
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR/.."
# The current directory is the application directory.

bin/rspec_dse spec/controllers/invoke_all_controller_spec.rb "$@" &
while [ ! -e "$DSE_TUNNEL_PATH" ]; do sleep 0.5; done
chmod 777 "$DSE_TUNNEL_PATH"
echo "Set permission on: $DSE_TUNNEL_PATH"

fg

