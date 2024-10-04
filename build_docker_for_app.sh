#!/usr/bin/env bash
set -ex

APP=${1?param missing - app}

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
DSE_DIR="$(dirname "$SCRIPT_DIR")"

if [ ! -d "$DSE_DIR/$APP" ]; then
    echo "Error: $DSE_DIR/$APP does not exist."
    exit 1
fi

dockerfile_path=$(mktemp)
trap 'rm -f "$dockerfile_path"' EXIT

cat > "$dockerfile_path" <<EOF
FROM eclipse-temurin:21
RUN apt update \\
  && apt install -y build-essential git curl gsfonts imagemagick libmagickwand-dev nodejs redis-server libssl-dev libcurl4-openssl-dev libxml2-dev libxslt1-dev default-libmysqlclient-dev mysql-server sudo

COPY ./jruby/bin /opt/jruby/bin
COPY ./jruby/lib/jni /opt/jruby/lib/jni
COPY ./jruby/lib/ruby /opt/jruby/lib/ruby
COPY ./jruby/lib/jruby.jar /opt/jruby/lib/
RUN update-alternatives --install /usr/local/bin/ruby ruby /opt/jruby/bin/jruby 1
ENV PATH=/opt/jruby/bin:\$PATH

COPY ./$APP /opt/$APP
COPY ./scripts/docker_entrypoint.sh /opt/$APP/bin/
RUN /opt/$APP/bin/set_up_dse_db

WORKDIR /opt/$APP
ENTRYPOINT ["bin/docker_entrypoint.sh"]
EOF

(cd "$DSE_DIR"; docker build -t "$APP-dse" -f "$dockerfile_path" .)
