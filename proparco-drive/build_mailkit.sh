#!/usr/bin/env bash
# build_mailkit.sh - build the lightweight "export and review my emails" kit to share.
#
#   ./build_mailkit.sh <destination-folder> [--zip]
#
# Copies from THIS program folder only what exporting and reviewing emails need: the portable
# Windows Python, six scripts, the settings and the three launchers, plus a one-page guide.
# No AI models, no data, no secrets (.env is never copied). The destination can be a flash disk.
#   EXPORT AND REVIEW MY EMAILS.bat   one click: export, then duplicate review
#   EXTRACT MY EMAILS.bat             export only
#   REVIEW DUPLICATE EMAILS.bat       review only (after an export)
set -euo pipefail
[ $# -ge 1 ] || { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }
DEST="${1%/}"; ZIP="${2:-}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"            # this proparco-drive
ROOT="$(dirname "$SRC")"                                           # where the launchers sit

need=(start.bat extract_mail.ps1 config/settings.json scripts/outlook_acquire.ps1 scripts/dedupe.py
      scripts/engine.py scripts/embed.py scripts/obs.py)
for f in "${need[@]}"; do [ -f "$SRC/$f" ] || { echo "Missing in $SRC: $f"; exit 1; }; done
[ -x "$SRC/python/win/python.exe" ] || [ -f "$SRC/python/win/python.exe" ] || {
  echo "The portable Windows Python ($SRC/python/win) is not in this folder."
  echo "Build the kit from a ready-to-run copy (e.g. the PROPARCO flash disk's proparco-drive)."; exit 1; }
for b in "EXPORT AND REVIEW MY EMAILS.bat" "EXTRACT MY EMAILS.bat" "REVIEW DUPLICATE EMAILS.bat"; do
  [ -f "$ROOT/$b" ] || [ -f "$SRC/$b" ] || { echo "Launcher not found beside $SRC: $b"; exit 1; }; done

K="$DEST/proparco-mailkit"
echo "Building the kit in $K ..."
rm -rf "$K"; mkdir -p "$K/proparco-drive/scripts" "$K/proparco-drive/config"
cp -r "$SRC/python" "$K/proparco-drive/"
rm -rf "$K/proparco-drive/python/lib-linux" 2>/dev/null || true      # Windows only
for f in "${need[@]}"; do [ "$f" = config/settings.json ] || cp -p "$SRC/$f" "$K/proparco-drive/$f"; done
python3 - "$SRC/config/settings.json" "$K/proparco-drive/config/settings.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8"))
for k in ("server_db_dir", "server_db_path", "sdrive_root", "sdrive_link_base"):   # nothing from this laptop
    c.pop(k, None)
json.dump(c, open(sys.argv[2], "w", encoding="utf-8"), indent=2, ensure_ascii=False)
PY
for b in "EXPORT AND REVIEW MY EMAILS.bat" "EXTRACT MY EMAILS.bat" "REVIEW DUPLICATE EMAILS.bat"; do
  cp -p "$( [ -f "$ROOT/$b" ] && echo "$ROOT/$b" || echo "$SRC/$b")" "$K/$b"; done
printf '%s\r\n' \
"EXPORT AND REVIEW MY EMAILS - read me first" \
"" \
"What it does: copies YOUR emails (mailbox and archives) from Outlook onto this flash disk," \
"read-only, then finds duplicate emails and attachments and shows them in your browser for review." \
"Nothing in Outlook is changed or deleted. Your emails stay on this flash disk, in" \
"proparco-data\\mailboxes\\<your Windows user name>, separate from anyone else's." \
"" \
"Before you start" \
"  1. Open classic Outlook (not the new Outlook) and leave it open." \
"  2. Old archives saved as .pst files: open them in Outlook first" \
"     (File > Open & Export > Open Outlook Data File)." \
"  3. Plug the laptop into power." \
"" \
"One click" \
"  Double-click EXPORT AND REVIEW MY EMAILS.bat" \
"  - If Outlook asks to allow access, click Allow." \
"  - Check that the list shows every mailbox and archive you use, then press Enter." \
"  - Large mailboxes take hours: leave the window and Outlook open." \
"  - At the end look for ALL ITEMS ACCOUNTED FOR. Then the review page opens in your browser." \
"" \
"The review page" \
"  Groups of duplicate copies, each with the copy proposed to keep. 'Compare text' shows them" \
"  side by side. Remove moves duplicates aside (Removed groups / restore puts them back)." \
"  Only the copies on this flash disk are affected, never Outlook." \
"" \
"Stopped or interrupted? Double-click again: it continues and never exports anything twice." \
> "$K/READ ME FIRST.txt"
( cd "$K" && find . -type f ! -name SHA256SUMS.txt -print0 | sort -z | xargs -0 sha256sum > proparco-drive/SHA256SUMS.txt )
echo "  $(du -sh "$K" | cut -f1), $(find "$K" -type f | wc -l) files; no models, no data, no .env"
if [ "$ZIP" = "--zip" ]; then
  Z="$DEST/proparco-mailkit-$(date +%Y%m%d).zip"; rm -f "$Z"
  ( cd "$DEST" && zip -qr "$(basename "$Z")" proparco-mailkit ) && echo "  zip: $Z ($(du -sh "$Z" | cut -f1))"
fi
echo "Share: the proparco-mailkit folder (or the zip). Each person double-clicks EXPORT AND REVIEW MY EMAILS.bat."
