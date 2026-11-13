#!/usr/bin/env python3
"""
summarize_selected.py - one click: the emails selected in Outlook -> summary -> deal page.

Started by summarize-selected.bat (double-click). Steps, all automatic:
  1. export the emails selected in classic Outlook (read-only), or with --latest N the N newest
     emails of the folder Outlook is showing (normally the Inbox)
  2. make them searchable
  3. summarise their thread(s)   (skipped with --fast: the deal summary reads the emails directly)
  4. refresh Notion (read-only, at most once a day) and link the thread(s) to their deal
  5. write the deal summary (Notion + emails + documents)
  6. open the interface (deal page, chat, evaluation) on that deal; it runs until the window is closed
Nothing is sent to Notion.
"""
import json
import platform
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from engine import load_config  # noqa: E402


def say(text=""):
    print(text, flush=True)


def run(script, *args):
    return subprocess.run([sys.executable, str(HERE / script), *args],
                          cwd=str(HERE.parent)).returncode == 0


def latest_outlook_run(data):
    runs = sorted((data / "acquisitions").glob("OUTLOOK-*/acquisition.json"),
                  key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def export_selected(data, latest=0):
    shell = shutil.which("powershell") or shutil.which("pwsh")
    which = ["-Selected"] if not latest else ["-DaysBack", "60", "-MaxMessages", str(latest)]   # newest first
    result = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(HERE / "outlook_acquire.ps1"), *which,
                             "-OutRoot", str(data / "acquisitions")], cwd=str(HERE.parent))
    return result.returncode == 0


def main():
    fast = "--fast" in sys.argv
    latest = int(sys.argv[sys.argv.index("--latest") + 1]) if "--latest" in sys.argv else 0
    cfg = load_config()
    data = cfg["_data_dir"]
    say("=" * 64)
    say(f" Summarise my {latest} latest emails" if latest else " Summarise the emails selected in Outlook")
    say("=" * 64)
    if platform.system() != "Windows":
        sys.exit("This needs Windows with classic Outlook.")

    say(f"\nStep 1 of 6: reading {'the ' + str(latest) + ' latest emails' if latest else 'the selected emails'} from Outlook (read-only) ...")
    before = latest_outlook_run(data)
    if not export_selected(data, latest):
        sys.exit("\nThe export did not finish. Is classic Outlook open"
                 + (", showing the Inbox?" if latest else ", with emails selected?"))
    latest = latest_outlook_run(data)
    if latest is None or latest == before:
        sys.exit("\nNo emails were exported. " + ("Is Outlook showing a folder with recent emails (the Inbox)?" if latest
                 else "Select one or more emails in Outlook, then try again."))
    msgs = json.loads(latest.read_text(encoding="utf-8-sig")).get("messages") or []
    msgs = msgs if isinstance(msgs, list) else [msgs]
    threads = sorted({m.get("conversationId") for m in msgs if m.get("conversationId")})
    say(f"   {len(msgs)} email(s), {len(threads)} conversation(s).")

    say("\nStep 2 of 6: making them searchable ...")
    run("embed.py")

    if fast:
        say("\nStep 3 of 6: skipped (fast mode: the deal summary reads the emails directly).")
    else:
        say(f"\nStep 3 of 6: summarising {len(threads)} conversation(s). "
            f"This takes about 1-5 minutes per email; you can keep working meanwhile.")
        for i, conv in enumerate(threads, 1):
            say(f"\n--- Conversation {i} of {len(threads)}")
            run("generate.py", "--thread", conv)

    say("\nStep 4 of 6: linking to the Notion deals ...")
    directory = data / "notion" / "directory.json"
    fresh = directory.exists() and \
        datetime.fromtimestamp(directory.stat().st_mtime) > datetime.now() - timedelta(days=1)
    if not fresh and not (fast and directory.exists()):      # fast mode uses the Notion copy on the stick
        say("   refreshing the Notion directory (read-only, about a minute) ...")
        if not run("notion_sync.py") and not directory.exists():
            say("   Notion could not be reached; showing the email summary only.")
    deals = []
    if directory.exists():
        run("notion_match.py")
        mpath = data / "notion" / "matches.json"
        matches = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else []
        for m in matches:
            if m["conversation_id"] in threads and m.get("candidates"):
                top = m["candidates"][0]
                if top["project_id"] not in [d["id"] for d in deals]:
                    deals.append({"id": top["project_id"], "name": top["deal"]})

    if deals:
        say(f"\nStep 5 of 6: writing the deal summary for {', '.join(d['name'] for d in deals)} ...")
        for d in deals:
            run("deal_summarize.py", "--deal", d["id"], "--retrieve", "--force")   # emails + server documents, checked
    else:
        say("\nStep 5 of 6: these emails did not match a Notion deal: no deal summary.")

    say("\nStep 6 of 6: opening the interface (deal page, chat, evaluation) ...")
    say("   Nothing was sent to Notion. Keep this window open while you use the page; close it to stop.")
    import os
    env = dict(os.environ, PROPARCO_OPEN=("#" + deals[0]["id"]) if deals else "")
    say(f"   It opens on: {deals[0]['name']}" if deals else "   No deal matched: ask the chat about these emails.")
    subprocess.run([sys.executable, str(HERE / "chat.py")], cwd=str(HERE.parent), env=env)


if __name__ == "__main__":
    started = time.time()
    try:
        main()
    except KeyboardInterrupt:
        say("\nStopped. What was finished is saved.")
    say(f"(took {(time.time() - started) / 60:.0f} min)")
