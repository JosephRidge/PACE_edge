#!/usr/bin/env python3
"""
menu.py - simple menu for running the pipeline. Started by start.sh / start.bat.
Every option runs one of the scripts in this folder with the same Python.
"""
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from engine import DRIVE_ROOT, load_config  # noqa: E402  (also sets UTF-8 console output)


def run(script, *args, pause=True):
    print()
    result = subprocess.run([sys.executable, str(HERE / script), *args], cwd=str(HERE.parent))
    if result.returncode not in (0, None):
        print(f"\n{script} stopped with an error (exit code {result.returncode}). "
              f"The message above says why; option 11 shows logged errors.")
    if pause:
        input("\nPress Enter to return to the menu ...")
    return result.returncode == 0


def ask(prompt):
    value = input(prompt).strip()
    if not value:
        print("Nothing entered.")
        input("Press Enter to return to the menu ...")
    return value or None


def ask_thread(action):
    """Show the threads first, so the user can copy an ID, then ask for one."""
    run("generate.py", "--list", pause=False)
    print(f"\nType the number of a thread in the list above (or the start of its ID) to {action}.")
    value = ask("Thread number or ID: ")
    if value and value.isdigit() and len(value) <= 4:
        value = "#" + value          # a list number, e.g. 12 -> #12
    return value


def run_outlook(*args):
    """Run the Outlook export in Windows PowerShell (classic Outlook must be open)."""
    if platform.system() != "Windows":
        print("\nThe Outlook export needs Windows with classic Outlook. "
              "On Linux, use Gmail or copy an export into the acquisitions folder.")
        input("\nPress Enter to return to the menu ...")
        return False
    shell = shutil.which("powershell") or shutil.which("pwsh")
    out = str(load_config()["_data_dir"] / "acquisitions")
    print()
    # No execution-policy bypass: if Windows blocks scripts, that is a decision for IT.
    result = subprocess.run([shell, "-NoProfile", "-File", str(HERE / "outlook_acquire.ps1"),
                             *args, "-OutRoot", out], cwd=str(HERE.parent))
    if result.returncode != 0:
        print("\nThe Outlook export did not finish. If Windows says running scripts is "
              "disabled, ask IT; do not change the execution policy on a work laptop.")
        input("\nPress Enter to return to the menu ...")
        return False
    return True


def ask_days():
    """Days back: a number only. Anything else is refused, never used or shown again."""
    while True:
        value = input("How many days back? [30] ").strip()
        if not value:
            return "30"
        if value.isdigit() and 0 < int(value) <= 3650:
            return value
        print("Please type a number of days (for example 30), or press Enter for 30.")
        print("(Never type a password here; it is only asked for at the password prompt.)")


def offer_embed():
    answer = input("\nEmbed the new emails now, so they can be searched and summarised? [Y/n] ")
    if answer.strip().lower() in ("", "y", "yes"):
        run("embed.py")
    else:
        input("\nPress Enter to return to the menu ...")


def fetch_menu():
    print("\nFetch emails")
    print("  1. Outlook: the emails selected in Outlook right now (recommended)")
    print("  2. Outlook: Inbox and Sent of the last N days")
    print("  3. Gmail: live mailbox (needs an app password)")
    print("  4. Gmail: Google Takeout .mbox file")
    print("  0. Back")
    choice = input("\nChoose a number: ").strip()
    if choice == "1":
        print("\nIn Outlook, select the emails first (Ctrl+click), then continue.")
        input("Press Enter when they are selected ...")
        ok = run_outlook("-Selected")
    elif choice == "2":
        ok = run_outlook("-DaysBack", ask_days())
    elif choice == "3":
        user = ask("Gmail address: ")
        if not user:
            return
        days = ask_days()
        query = input('Optional Gmail search, e.g. label:proparco [none]: ').strip()
        print("\nNext you will be asked for a Google APP PASSWORD (16 letters, from")
        print("myaccount.google.com/apppasswords), not your normal Google password.")
        args = ["imap", "--user", user, "--days", days]
        if query:
            args += ["--query", query]
        ok = run("gmail_acquire.py", *args, pause=False)
    elif choice == "4":
        path = ask("Full path of the .mbox file: ")
        if not path:
            return
        ok = run("gmail_acquire.py", "mbox", "--file", path.strip('"'),
                 "--days", ask_days(), pause=False)
    else:
        return
    if ok:
        offer_embed()
    else:
        print("\nNothing new was fetched, so there is nothing to embed.")
        input("Press Enter to return to the menu ...")


def _latest_outlook_run(data):
    runs = sorted((data / "acquisitions").glob("OUTLOOK-*/acquisition.json"),
                  key=lambda p: p.stat().st_mtime)
    return runs[-1] if runs else None


def quick_run():
    """Selected Outlook emails -> fetch -> embed -> summarise those threads -> viewer."""
    cfg = load_config()
    data = cfg["_data_dir"]
    print("\nQuick run: the emails selected in Outlook will be fetched, made searchable,")
    print("summarised (only the threads you selected) and shown in the browser.")
    if platform.system() != "Windows":
        print("\nThis needs Windows with classic Outlook. On Linux use option 15 (Gmail).")
        input("\nPress Enter to return to the menu ...")
        return
    before = _latest_outlook_run(data)
    print("\nStep 1 of 4: in Outlook, select the emails (Ctrl+click whole conversations).")
    input("Press Enter when they are selected ...")
    if not run_outlook("-Selected"):
        return
    latest = _latest_outlook_run(data)
    if latest is None or latest == before:
        print("\nNo new export was found, so nothing more to do.")
        input("\nPress Enter to return to the menu ...")
        return
    with open(latest, encoding="utf-8-sig") as f:
        msgs = json.load(f).get("messages") or []
    msgs = msgs if isinstance(msgs, list) else [msgs]
    threads = sorted({m.get("conversationId") for m in msgs if m.get("conversationId")})
    print(f"\nStep 2 of 4: making {len(msgs)} emails searchable ...")
    run("embed.py", pause=False)
    minutes = max(1, round(len(msgs) * 1.5))
    print(f"\nStep 3 of 4: summarising {len(threads)} thread(s). "
          f"Allow about {minutes} minute(s); you can keep working meanwhile.")
    if input("Start now? [Y/n] ").strip().lower() not in ("", "y", "yes"):
        print("Skipped. You can summarise later with option 6 or 7.")
        input("\nPress Enter to return to the menu ...")
        return
    for i, conv in enumerate(threads, 1):
        print(f"\n--- Thread {i} of {len(threads)}")
        run("generate.py", "--thread", conv, pause=False)
    print("\nStep 4 of 4: opening the results in your browser ...")
    run("viewer.py")


OPTIONS = [
    ("Health check (models, embeddings, speed)", lambda: run("engine.py", "test")),
    ("Embed new emails: check only (dry run)", lambda: run("embed.py", "--dry-run")),
    ("Embed new emails", lambda: run("embed.py")),
    ("List email threads", lambda: run("generate.py", "--list")),
    ("Estimate summary generation time (dry run)", lambda: run("generate.py", "--dry-run")),
    ("Generate summaries for ONE thread", lambda: (lambda t: run("generate.py", "--thread", t)
                                                  if t else None)(ask_thread("generate it"))),
    ("Generate summaries for ALL new emails (long)", lambda: run("generate.py", "--all")),
    ("Show results for a thread", lambda: (lambda t: run("generate.py", "--show", t)
                                          if t else None)(ask_thread("show its results"))),
    ("Search emails and summaries", lambda: (lambda q: run("search.py", q)
                                            if q else None)(ask("Search for: "))),
    ("Run report (times, tokens, errors)", lambda: run("obs.py", "report")),
    ("Recent errors and warnings", lambda: run("obs.py", "errors")),
    ("Open the log dashboard (charts, in the browser)", lambda: run("obs.py", "dashboard")),
    ("Quality evaluation (gold set)", lambda: run("eval.py")),
    ("Browse threads in the browser (summaries, people, attachments)", lambda: run("viewer.py")),
    ("Fetch emails (Outlook or Gmail), then embed them", fetch_menu),
    ("Quick run: selected Outlook emails -> summaries -> browser", quick_run),
]


def _same_disk(a, b):
    """True when two paths are on the same disk (compares device IDs; works on
    Windows and Linux). Missing paths are checked through their nearest existing parent."""
    def device(p):
        p = Path(p)
        while not p.exists() and p != p.parent:
            p = p.parent
        return os.stat(p).st_dev
    try:
        return device(a) == device(b)
    except OSError:
        return False


def _on_removable_media(path):
    """Best effort: is this path on a USB stick or similar? Windows asks the drive type;
    Linux checks the usual mount locations for removable media."""
    path = Path(path).resolve()
    if platform.system() == "Windows":
        try:
            import ctypes
            drive = path.drive + "\\"
            return ctypes.windll.kernel32.GetDriveTypeW(drive) == 2   # DRIVE_REMOVABLE
        except Exception:
            return False
    return str(path).startswith(("/media/", "/run/media/", "/Volumes/"))


def stick_guard(cfg):
    """Warn (and stop unless confirmed) if client data would be written onto the stick."""
    data = cfg["_data_dir"]
    # Running from a stick and data on that same stick: client emails would be carried around.
    # Running the package from the laptop's own disk (e.g. if USB is blocked) is fine.
    if _on_removable_media(DRIVE_ROOT) and _same_disk(data, DRIVE_ROOT):
        print("\n" + "!" * 60)
        print(" WARNING: the data folder is on the same disk as this program:")
        print(f"   {data}")
        print(" Client emails must stay on the laptop, never on the flash disk.")
        print(' Set "data_dir" in config/settings.json to a folder on the laptop,')
        print(' for example "~/proparco-data", then start again.')
        print("!" * 60)
        if input("\nContinue anyway? [y/N] ").strip().lower() != "y":
            sys.exit(1)


def first_run_check(cfg):
    data = cfg["_data_dir"]
    stick_guard(cfg)
    if data.exists():
        return
    print("\n" + "=" * 60)
    print(" Welcome. This looks like the first run on this computer.")
    print("=" * 60)
    print(f"\nYour emails, results and logs will be kept in:\n   {data}")
    print("This folder is on this laptop's own disk, not on the flash disk.")
    if input("\nCreate it now? [Y/n] ").strip().lower() in ("", "y", "yes"):
        for sub in ("acquisitions", "db", "logs"):
            (data / sub).mkdir(parents=True, exist_ok=True)
        print("\nCreated. Suggested first steps:")
        print("   1  Health check: confirms the AI models work on this computer")
        print("  15  Fetch emails: select them in Outlook first")
        input("\nPress Enter to open the menu ...")
    else:
        print("\nNothing created. Ask the project team if you were given a data folder to copy here.")
        input("Press Enter to open the menu ...")


def status_line(cfg):
    """One line telling the user where they stand, and what to do next."""
    data = cfg["_data_dir"]
    if not data.exists():
        return "No data folder yet"
    runs = list((data / "acquisitions").glob("*/acquisition.json")) if (data / "acquisitions").exists() else []
    db_path = data / "db" / "proparco.db"
    if not runs and not db_path.exists():
        return "No emails yet: choose 15 to fetch emails"
    if not db_path.exists():
        return f"{len(runs)} fetch(es) waiting to be embedded: choose 3"
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        emails = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        done = review = 0
        if "email_results" in tables:
            done, review = db.execute("SELECT COUNT(*), COALESCE(SUM(review_status = "
                                      "'needs_review'), 0) FROM email_results").fetchone()
        db.close()
    except sqlite3.Error:
        return "Database could not be read: choose 11 for errors"
    line = f"{emails} emails · {done} with summaries"
    if review:
        line += f" · {review} need review"
    if emails and not done:
        line += ": choose 6 for one thread or 7 for all"
    return line


def main():
    cfg = load_config()
    if "--menu" not in sys.argv and cfg.get("start_mode", "menu") == "auto":
        stick_guard(cfg)
        import autorun                      # plug-and-play client flow, no questions
        autorun.main()
        return
    first_run_check(cfg)
    if platform.system() == "Windows" and cfg.get("quick_run_on_start", True):
        if input("\nProcess the emails selected in Outlook now? [Y/n] ").strip().lower() in ("", "y", "yes"):
            quick_run()
    while True:
        print("\n" + "=" * 60)
        print(" Proparco email assistant (local)")
        print(f" Data folder: {cfg['_data_dir']}")
        print(f" Status: {status_line(cfg)}")
        print("=" * 60)
        for i, (label, _) in enumerate(OPTIONS, 1):
            print(f" {i:>2}. {label}")
        print("  0. Exit")
        choice = input("\nChoose a number: ").strip()
        if choice in ("0", "q", "exit"):
            return
        if choice.isdigit() and 1 <= int(choice) <= len(OPTIONS):
            OPTIONS[int(choice) - 1][1]()
        else:
            print("Please type one of the numbers shown.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()