#!/bin/sh
# Demo run: only the 2 newest emails (fetch, summarise, show). Change the number if needed.
cd "$(dirname "$0")" || exit 1
PROPARCO_DEMO_EMAILS=2 exec sh start.sh