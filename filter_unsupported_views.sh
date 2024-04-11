#!/usr/bin/env bash
grep -v "LEFT JOIN" | sed 's/TIMESTAMPADD(SECOND, 1, _NOW)/_NOW/g'

