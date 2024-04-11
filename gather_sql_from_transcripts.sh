#!/usr/bin/env bash
if [[ $# -eq 0 ]] ; then
    echo "Usage: $0 directory"
    exit 1
fi

DIR=$1

cat $DIR/transcript-*.json.gz | gunzip -c | jq -r '.elements[] | select(.sqlQueryDecl) | .sqlQueryDecl.query' | sort -u

