# Proparco email assistant (local AI, runs from a flash disk)

Reads Outlook emails and writes three CRM fields per email, entirely on the laptop:
**Latest Deal Summary**, **Project History** and **Next Steps**.
The AI runs offline from a flash disk; email content never leaves the computer.

> Field names will be aligned with the Notion database at the Notion step.

## Received a ready flash disk? (clients)

1. Copy the `proparco-data` folder to your home folder
   (Windows: type `%USERPROFILE%` in File Explorer's address bar).
2. Open `proparco-drive` on the flash disk and start it:
   **Windows** double-click `start.bat`, **Linux** `sh start.sh`.
3. Choose **1** (health check) the first time, then **14** to browse the results in your browser.
4. Choose **0** to exit, and eject the flash disk before unplugging.

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