#!/usr/bin/env bash
if [[ $# -eq 0 ]] ; then
    echo "Usage: $0 directory"
    exit 1
fi

DIR=$1

cat $DIR/paths-with-conds-*.json.gz | gunzip -c | jq -r '.[] | .[0] | select(.["$type"] == "SqlQueryDecl") | .query' | sort -u

