#!/bin/sh
# Demo run: the newest email only, one thread (fetch, summarise, show).
cd "$(dirname "$0")" || exit 1
PROPARCO_DEMO_EMAILS=1 exec sh start.sh