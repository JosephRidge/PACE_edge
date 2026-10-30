# Proparco email assistant (local AI, runs from a flash disk)

Reads Outlook emails and writes three CRM fields per email, entirely offline from a flash disk:
**Latest Deal Summary**, **Project History** and **Next Steps**.
The AI runs offline from a flash disk; email content never leaves the computer.

> Field names will be aligned with the Notion database at the Notion step.

> **This repository is the source code.** It does not include the AI models or the portable
> Python, so `start.bat` does not run from a clone. To use the assistant, take the built package
> (flash disk or zip from the project team); to make one, follow the builder steps below.

## Received a ready flash disk? (clients)

1. Keep classic Outlook open. On the flash disk, open `proparco-drive` and double-click
   **`start.bat`** (Linux: `sh start.sh`).
2. No questions: it fetches new emails, summarises them and opens the results in your browser.
   The emails and results are stored on the flash disk itself (`proparco-data`), so they travel
   with the stick: keep it safe, and use an encrypted stick (BitLocker To Go).
3. When it says *Done*, close the window and eject the flash disk.

Testers: **`advanced-menu.bat`** opens the full menu (health check, search, logs, Gmail, ...).

Full client guide: [SETUP.md, Part 1](SETUP.md#part-1-clients-plug-and-play).

## Setting it up from scratch? (builders and testers)

```bash
git clone <repo-url> && cd <repo-folder>
python3 -m venv env && source env/bin/activate && pip install sqlite-vec
cd proparco-drive
curl -L -o bin/llamafile-0.10.6-thin https://github.com/mozilla-ai/llamafile/releases/download/0.10.6/llamafile-0.10.6-thin
curl -L -o models/Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile https://huggingface.co/mozilla-ai/llamafile_0.10/resolve/main/Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile
curl -L -o models/bge-m3-q8.gguf https://huggingface.co/gpustack/bge-m3-GGUF/resolve/main/bge-m3-Q8_0.gguf
chmod +x bin/llamafile-0.10.6-thin models/*.llamafile
# set data_dir and internal_domains in config/settings.json, then:
python3 scripts/engine.py test                     # check the AI engines
python3 build_package.py --out /media/<you>/<disk> # build a flash disk
```

Full builder guide (testing, database, troubleshooting): [SETUP.md, Part 2](SETUP.md#part-2-builders-and-testers).

**A flash disk holds only the built `proparco-drive` folder: never emails, databases or test data.**
What belongs on it, and how to check a disk before handing it over: [SETUP.md, section 2.5](SETUP.md#25-build-a-flash-disk).