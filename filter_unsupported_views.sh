#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/format_views.py" \
    | grep -v "LEFT JOIN" \
    | grep -v "WHERE FALSE;" \
    | sed 's/TIMESTAMPADD(SECOND, 1, _NOW)/_NOW/g'

