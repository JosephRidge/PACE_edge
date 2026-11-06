#!/usr/bin/env python3
"""
autorun.py - the plug-and-play flow that start.bat runs for clients. No menu, no questions:

  1. first use only: create the data folder on this laptop
  2. fetch NEW emails from Outlook (first time: the last N days; then: since the last run)
  3. make them searchable (embed)
  4. summarise only the new emails (Latest Deal Summary, Project History, Next Steps)
  5. open the results in the browser

Everything is incremental: nothing is processed twice, and if the window is closed during
a long run, the next run continues where it stopped.

Settings (config/settings.json):
  "start_mode": "auto"            "auto" = this flow; "menu" = the full menu
  "auto_source": "outlook"        "outlook" or "none" (skip fetching, only process)
  "auto_first_fetch_days": 30     how far back the very first fetch goes
  "demo_max_emails": 0            demo: only the N newest emails (0 = off); demo.bat sets 1.
                                  A demo always summarises ONE thread, for speed.
Testers open the full menu with advanced-menu.bat (or: sh start.sh menu.py --menu).
"""
import json
import math
import os
import platform
import sqlite3
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from engine import load_config  # noqa: E402


def say(text=""):
    print(text, flush=True)


def run(script, *args):
    result = subprocess.run([sys.executable, str(HERE / script), *args], cwd=str(HERE.parent))
    return result.returncode == 0


def days_to_fetch(data, first_days):
    """Days back for this fetch: since the newest Outlook fetch (plus a day of overlap;
    duplicates are skipped later), or the first-run setting."""
    newest = None
    for manifest in (data / "acquisitions").glob("OUTLOOK-*/manifest.json"):
        try:
            with open(manifest, encoding="utf-8-sig") as f:
                when = datetime.fromisoformat(json.load(f)["acquiredAt"].replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            newest = when if newest is None or when > newest else newest
        except (OSError, ValueError, KeyError):
            continue
    if newest is None:
        return first_days, True
    gap = (datetime.now(timezone.utc) - newest).total_seconds() / 86400
    return max(1, math.ceil(gap) + 1), False


def demo_limit(cfg):
    try:
        return max(0, int(os.environ.get("PROPARCO_DEMO_EMAILS") or cfg.get("demo_max_emails", 0) or 0))
    except ValueError:
        return 0


def latest_outlook_run(data):
    runs = sorted((data / "acquisitions").glob("OUTLOOK-*/acquisition.json"),
                  key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def demo_threads(data, limit, fetched):
    """Threads to summarise in demo mode: those of the emails just fetched, or else the
    threads of the newest emails that have no summary yet."""
    if fetched is not None:
        with open(fetched, encoding="utf-8-sig") as f:
            msgs = json.load(f).get("messages") or []
        msgs = msgs if isinstance(msgs, list) else [msgs]
        return list(dict.fromkeys(m.get("conversationId") for m in msgs if m.get("conversationId")))
    db_path = data / "db" / "proparco.db"
    if not db_path.exists():
        return []
    db = sqlite3.connect(db_path)
    has_results = db.execute("SELECT 1 FROM sqlite_master WHERE name='email_results'").fetchone()
    query = ("SELECT conversation_id FROM messages m "
             + ("WHERE NOT EXISTS (SELECT 1 FROM email_results r WHERE r.entry_id = m.entry_id) "
                if has_results else "")
             + "ORDER BY COALESCE(received_iso, received_raw) DESC LIMIT ?")
    rows = [r[0] for r in db.execute(query, (limit,))]
    db.close()
    return list(dict.fromkeys(rows))


def fetch_outlook(data, days, max_messages=None):
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        say("   PowerShell was not found, so emails cannot be fetched from Outlook.")
        return False
    args = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(HERE / "outlook_acquire.ps1"),
            "-DaysBack", str(days), "-OutRoot", str(data / "acquisitions")]
    if max_messages:
        args += ["-MaxMessages", str(max_messages)]
    result = subprocess.run(args, cwd=str(HERE.parent))
    if result.returncode != 0:
        say("\n   Fetching from Outlook did not work (see the message above).")
        say("   Check that classic Outlook is open. If scripts are still blocked, the")
        say("   policy is enforced by IT. Processing continues with emails already fetched.")
        return False
    return True


def main():
    cfg = load_config()
    data = cfg["_data_dir"]
    demo = demo_limit(cfg)
    say("=" * 64)
    say(" Proparco email assistant" + ("  (DEMO: newest email, one thread)" if demo else ""))
    say("=" * 64)

    if not data.exists():
        for sub in ("acquisitions", "db", "logs"):
            (data / sub).mkdir(parents=True, exist_ok=True)
        say(f"\nFirst use: created the data folder\n   {data}")
        if cfg.get("data_on_drive"):
            say("   (on the flash disk, next to the program: the emails travel with the stick,")
            say("    so keep the stick safe; an encrypted stick is recommended)")
        else:
            say("   (on this laptop's own disk; nothing is stored on the flash disk)")

    # 1. fetch
    before = latest_outlook_run(data)
    source = cfg.get("auto_source", "outlook")
    if source == "outlook":
        days, first = days_to_fetch(data, cfg.get("auto_first_fetch_days", 30))
        if demo:
            say(f"\nStep 1 of 4: fetching the {demo} newest email(s) from Outlook ...")
        else:
            say(f"\nStep 1 of 4: fetching {'the last' if first else 'new emails from the last'} "
                f"{days} day(s) from Outlook (Inbox and Sent) ...")
        if platform.system() != "Windows":
            say("   Outlook fetching needs Windows; skipped. Use advanced-menu for Gmail.")
        else:
            fetch_outlook(data, days, max_messages=demo or None)
    else:
        say("\nStep 1 of 4: fetching is switched off; processing emails already fetched.")

    if not any((data / "acquisitions").glob("*/acquisition.json")):
        say("\nNo emails have been fetched yet, so there is nothing to process.")
        input("\nPress Enter to close ...")
        return

    # 2. embed
    say("\nStep 2 of 4: making new emails searchable ...")
    if not run("embed.py"):
        say("\nThis step stopped with an error; the message above says why.")
        input("\nPress Enter to close ...")
        return

    # 3. summarise
    say("\nStep 3 of 4: writing summaries for new emails.")
    say("   This takes about 1 to 2 minutes per new email; you can keep working.")
    say("   If you close this window, the next start continues where it stopped.\n")
    if demo:
        latest = latest_outlook_run(data)
        fetched = latest if latest is not None and latest != before else None
        threads = demo_threads(data, demo, fetched)[:1]   # a demo covers one thread only
        if not threads:
            say("   Demo: no emails without a summary were found.")
        for i, conv in enumerate(threads, 1):
            say("--- Demo: summarising one thread")
            run("generate.py", "--thread", conv)
    else:
        run("generate.py", "--all")

    # 4. results
    say("\nStep 4 of 4: opening the results in your browser ...")
    run("viewer.py")
    say("\nDone. Close this window, then eject the flash disk before unplugging it.")
    input("\nPress Enter to close ...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped. Everything finished so far is saved; the next start continues.")