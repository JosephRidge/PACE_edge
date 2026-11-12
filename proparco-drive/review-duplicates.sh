#!/usr/bin/env bash
# review-duplicates.sh - preview the duplicate-email review on THIS machine, safely.
#
#   ./review-duplicates.sh <exported-mailbox-folder>               # copy emails, scan, open the page
#   ./review-duplicates.sh <exported-mailbox-folder> --scan-only   # copy emails, print the report
#   ./review-duplicates.sh <exported-mailbox-folder> --attachments # also compare attachments (read
#                                                                  #   from the source, never copied)
#   ./review-duplicates.sh <folder> --in-place                     # work directly on <folder>
#                                                                  #   (only for a folder that is already a copy)
# <exported-mailbox-folder> is the folder that contains "acquisitions", e.g.
#   /media/me/STICK/mails/OLIVIA-ADMINCROSSS/proparco-data
#   /media/me/STICK/ETL/proparco-data/mailboxes/DIRECTOR
#
# By default the SOURCE IS NEVER CHANGED: the emails (acquisition.json and manifest.json, no
# attachments, no index) are copied to ~/proparco-work/review/<name>/ and reviewed there. Even a
# 50+ GB mailbox copies quickly, since nearly all of that size is attachments. Running it again
# refreshes the copy and keeps the decisions already made there.
set -euo pipefail

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
[ $# -ge 1 ] || usage
SRC="${1%/}"; shift
SCAN_ONLY=""; IN_PLACE=""; ATT=""; PORT=8766
while [ $# -gt 0 ]; do
  case "$1" in
    --scan-only) SCAN_ONLY="--scan-only" ;;
    --in-place)  IN_PLACE=1 ;;
    --attachments) ATT="--attachments" ;;
    --port)      PORT="$2"; shift ;;
    *) usage ;;
  esac; shift
done

[ -d "$SRC/acquisitions" ] || { echo "No 'acquisitions' folder in: $SRC"; echo "Point it at the exported mailbox folder (the one containing 'acquisitions')."; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"           # the proparco-drive folder
[ -f "$HERE/scripts/dedupe.py" ] || { echo "dedupe.py not found next to this script ($HERE/scripts)."; exit 1; }

# ---------------------------------------------------------------- the working copy
if [ -n "$IN_PLACE" ]; then
  WORK="$SRC"
  echo "Working directly on: $WORK"
else
  NAME="$(basename "$(dirname "$SRC")")-$(basename "$SRC")"
  NAME="$(echo "$NAME" | tr -c 'A-Za-z0-9._-' '_' | sed 's/_*$//')"
  WORK="$HOME/proparco-work/review/$NAME"
  mkdir -p "$WORK/acquisitions"
  echo "Source (never changed): $SRC"
  echo "  size with attachments: $(du -sh "$SRC" 2>/dev/null | cut -f1)"
  echo "Copying the emails only (no attachments, no index) to: $WORK ..."
  # export folders already reviewed here keep their removed-duplicates.json and backups;
  # only the source's acquisition.json files are refreshed where nothing was removed yet
  for d in "$SRC"/acquisitions/*/; do
    run="$(basename "$d")"; mkdir -p "$WORK/acquisitions/$run"
    if [ -f "$WORK/acquisitions/$run/removed-duplicates.json" ]; then continue; fi
    for f in acquisition.json manifest.json; do
      [ -f "$d$f" ] && cp -p "$d$f" "$WORK/acquisitions/$run/$f"
    done
  done
  echo "  working copy: $(du -sh "$WORK" | cut -f1), $(ls "$WORK/acquisitions" | wc -l) export folder(s)"
fi

# ---------------------------------------------------------------- python environment
for env in "$HERE/../env" "$HERE/env" "$HERE/.venv"; do
  if [ -f "$env/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$env/bin/activate"; echo "Python environment: $env"; break
  fi
done
command -v python3 >/dev/null || { echo "python3 not found."; exit 1; }
python3 -c "import sqlite_vec" 2>/dev/null || echo "Note: sqlite-vec not available: scanning and the page work; removing from an index would not (this copy has no index anyway)."

# ---------------------------------------------------------------- run
export PROPARCO_DATA_DIR="$WORK"
export PROPARCO_ATTACH_ROOT="$SRC"          # attachments are read from the source, never copied
N=$(python3 - "$WORK" <<'EOF'
import json, glob, sys
n = 0
for f in glob.glob(sys.argv[1] + "/acquisitions/*/acquisition.json"):
    try: n += len(json.load(open(f, encoding="utf-8-sig")).get("messages") or [])
    except Exception: pass
print(n)
EOF
)
echo "Emails to scan: $N  (about $(( N / 3500 + 1 )) min on this laptop)"
cd "$HERE"
[ -n "$ATT" ] && echo "Attachments: read from the source (first run reads every file once; later runs use the cache)."
exec python3 -B scripts/dedupe.py $SCAN_ONLY $ATT --port "$PORT"
