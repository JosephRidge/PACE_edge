#!/bin/sh
# Proparco email assistant - Linux / macOS launcher.
# Usage:  ./start.sh                  (menu)
#         ./start.sh generate.py --list   (run one script directly)
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then PY=python3
elif command -v python >/dev/null 2>&1; then PY=python
else echo "Python 3 is required. Install it with your package manager (e.g. sudo apt install python3)."; exit 1
fi

# Bundled packages (sqlite-vec) for Linux live on the drive; nothing is installed system-wide.
export PYTHONPATH="$(pwd)/python/lib-linux${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUTF8=1
# Local model servers must never go through a proxy.
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"

if [ $# -eq 0 ]; then
    exec "$PY" scripts/menu.py
else
    script="$1"; shift
    exec "$PY" "scripts/$script" "$@"
fi