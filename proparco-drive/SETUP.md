# Proparco email assistant: setup and use

A local AI assistant that reads Outlook emails and writes three CRM fields per email:
**Latest Deal Summary**, **Project History** and **Next Steps**.

- The AI models run **offline from a flash disk**. Email content never leaves the computer.
- Every generated fact is **checked against the email**; anything unverifiable is held for review.
- Results are stored in a **local database** and reviewed before anything goes to Notion.

> The three field names will be aligned with the Notion database at the Notion step.

**Which part do you need?**

- **[Part 1: Clients](#part-1-clients-plug-and-play)**: you received a ready flash disk. Plug in and use.
- **[Part 2: Builders and testers](#part-2-builders-and-testers)**: you set it up from scratch,
  test it, or build flash disks.

---

# Part 1: Clients (plug and play)

Nothing to install. Everything needed is on the flash disk.

## What you need

- Windows 10 or 11 (64-bit), or Linux
- 8 GB of memory (16 GB is better)
- The flash disk
- Classic Outlook, open and signed in (to fetch emails)

## First time only

The first time you start the assistant on a computer, it offers to create its data folder:
`C:\Users\<you>\proparco-data` on Windows, `~/proparco-data` on Linux. Answer **Y**.

This folder holds your emails, results and logs. It is on your computer's own disk, never on
the flash disk, and it starts empty: the assistant only ever works with emails you fetch
yourself (option 15).

## Every time: plug in and double-click

1. Plug in the flash disk, open `proparco-drive` and double-click **`start.bat`**
   (Linux: `sh start.sh`). Keep classic Outlook open.
2. That is all. Without any questions it fetches **new emails** from Outlook (the first time:
   the last 30 days; afterwards: everything since the last run), makes them searchable,
   writes the three fields for the new emails and opens the results in your browser.
3. The first run can take a while (about 1 to 2 minutes per email); you can keep working.
   If you close the window, the next start continues where it stopped.
4. When it says *Done*, close the window and eject the flash disk.

## The full menu (testers and advanced use)

Double-click **`advanced-menu.bat`** (Linux: `sh advanced-menu.sh`) for every function
separately:

1. Plug in the flash disk and open the `proparco-drive` folder on it.
2. Start the assistant:
   - **Windows:** double-click `start.bat`
   - **Linux:** open a terminal in that folder and type `sh start.sh`
3. On Windows, it first asks **"Process the emails selected in Outlook now?"** Select the
   emails in Outlook (Ctrl+click), answer **Y**, and it fetches them, makes them searchable,
   summarises those threads and opens the results in your browser. Answer **N** to go straight
   to the menu.
4. The menu shows the data folder and a status line (what to do next). Type a number and
   press Enter.

The first time, choose **1 (Health check)**. It should end with `Embedding check: PASS`.
Loading the models from the flash disk can take a minute.

Then fetch your first emails: in Outlook, select a few emails of one deal (Ctrl+click), and
choose **15 → 1**. Accept the offer to embed them, then use **6** to generate the three fields
and **14** to read them. The status line at the top of the menu always says what to do next.

## The menu

| Number | What it does |
|---|---|
| 1 | Health check: are the AI models working, and how fast |
| 4 | List email threads |
| 5 | Estimate how long generating all summaries would take |
| 6 | Generate the three fields for **one** thread (about 1-2 minutes per email) |
| 7 | Generate for **all** new emails (can take many hours; run it overnight) |
| 8 | Show the three fields for a thread |
| 9 | Search the emails by meaning, in any language |
| 10 | Run report: times, tokens, errors |
| 11 | Recent errors and warnings |
| 12 | Open the log dashboard (charts) in the browser |
| 13 | Quality evaluation against the hand-written facts |
| 14 | Browse threads in the browser: title, people, the three fields, emails, attachment links |
| 15 | Fetch emails: the ones selected in Outlook, the last N days, or Gmail; then embed them |
| 16 | Quick run: the emails selected in Outlook → searchable → summarised → shown in the browser |
| 0 | Exit |

Options 6 and 8 first list the threads; type a thread's number from that list (or the start of its ID).

**Getting new emails (option 15):** in Outlook, select the emails you want (Ctrl+click), then choose 15 → 1. The menu exports them, then offers to embed them. Classic Outlook must be open; the "new Outlook" app is not supported.

## Reading the results

Option 8 shows, for each email in a thread:

- **Latest Deal Summary**: the current state of the deal. **NEW UPDATE!** marks the email
  that changed it. "Awaiting future deal" means no deal information yet.
- **Next Steps**: `-` open actions and who must act; `✓` completed actions.
- **Project History**: one dated line per email, oldest first. `(approx.)` means the
  original date of a forwarded email was unclear.
- **review: needs_review**: a name, number or source in the notes could not be found in the
  email. A person should check it before it goes to Notion.

## When you finish

Choose **0**, then **eject** the flash disk before unplugging it
(Windows: the USB icon at the bottom right; Linux: eject in the file manager).
Unplugging without ejecting can make the disk unreadable on Linux until it is repaired.

## If something goes wrong

| You see | Do this |
|---|---|
| The antivirus blocks or removes a file | **Stop.** Contact the project team or IT. Do not try to get around it |
| "Windows protected your PC" | Do not click "Run anyway" on a work laptop. Contact the project team |
| A firewall question about `llamafile` | Click **Cancel**. It only talks to this computer |
| "No database" or no threads listed | No emails fetched yet, or the data folder is elsewhere. Check the status line and path at the top of the menu; fetch emails with **15** |
| Strange symbols instead of ✓ or — | Display only. The stored results are correct |
| Anything else | Choose **11** and send the message shown to the project team |

---

# Part 2: Builders and testers

## 2.1 How it works

```
Outlook ──► acquisition.json ──► embed.py ──────► generate.py ─────► review ──► Notion
            (PowerShell,         clean, chunk,     Ministral writes   approve    (later step)
             next step)          bge-m3 vectors    facts; Python
                                 into sqlite-vec   applies the rules
                                                   and verifies them
```

| Part | What | Where |
|---|---|---|
| AI engine | `llamafile-0.10.6-thin`, one program for Linux and Windows (`.exe` copy) | `bin/` |
| Writing model | Ministral 3 3B Instruct, Q4_K_M | `models/`, served on `127.0.0.1:8081` |
| Search model | BAAI bge-m3, Q8_0, 1024 dimensions | `models/`, served on `127.0.0.1:8082` |
| Pipeline | `engine.py` `embed.py` `generate.py` `verify.py` `search.py` `obs.py` `eval.py` `menu.py` | `scripts/` |
| Settings | `settings.json`, `eval_set.json` | `config/` |
| Data | emails, database, logs, dashboard | `proparco-data/`, **outside** the drive folder |

**Why two folders?** If the flash disk is lost, it only holds programs and public models.

The servers are started and stopped automatically by the scripts; you never run them by hand.
They listen on `127.0.0.1` only, use a new random API key on every run, and run CPU-only
with llamafile's sandbox switched on.

## 2.2 Set up from scratch

Develop on Linux. Windows is tested through the built flash disk (2.5).

**1. Code and Python environment**

```bash
git clone <repo-url>
cd <repo-folder>
python3 -m venv env && source env/bin/activate
pip install sqlite-vec
cd proparco-drive
```

**2. Download the three AI files**

```bash
curl -L -o bin/llamafile-0.10.6-thin \
  https://github.com/mozilla-ai/llamafile/releases/download/0.10.6/llamafile-0.10.6-thin
curl -L -o models/Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile \
  https://huggingface.co/mozilla-ai/llamafile_0.10/resolve/main/Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile
curl -L -C - --retry 5 -o models/bge-m3-q8.gguf \
  https://huggingface.co/gpustack/bge-m3-GGUF/resolve/main/bge-m3-Q8_0.gguf
chmod +x bin/llamafile-0.10.6-thin models/*.llamafile
```

If a download stops early, run the same command again with `-C -` (resume).

**3. Check the downloads**

```bash
ls -lh bin/ models/
sha256sum models/bge-m3-q8.gguf
```

| File | Expected size (`ls -lh`) |
|---|---|
| `Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile` | about 3.0G (3.4 GB) |
| `bge-m3-q8.gguf` | about 606M (635 MB) |

A file of only a few MB means the download failed. Recorded SHA256 of `bge-m3-q8.gguf`:
`950f4a8e5e19477a6d3c26d2f162233c20002c601f75e4b002e3239997821167`

**4. Settings** (`config/settings.json`)

Set at least:

- `data_dir`: the full path of your data folder, for example
  `"~/Documents/.../proparco-data"`. Use a full path, not `../`, so data never lands on a drive.
- `internal_domains`: your organisation's email domains, e.g. `["proparco.fr", "afd.fr"]`.

**5. Data**

Clients always start empty and fetch fresh emails (menu option 15). For development and
evaluation, you may use a test data folder in `data_dir` (`acquisitions/`, `db/`, `logs/`).
Test data contains real client emails: keep it on your own machine only, never on a flash
disk or a client laptop, and delete it when the project ends or as agreed with the client.

## 2.3 Test, in stages

Run from `proparco-drive/`, with heavy programs and virtual machines closed.

| Stage | Command | Passes when |
|---|---|---|
| Engines | `python3 scripts/engine.py test` | `Embedding check: PASS`, speeds printed |
| Embedding (check) | `python3 scripts/embed.py --dry-run` | counts look right; nothing unexpected to process |
| Embedding | `python3 scripts/embed.py` | finishes; can be stopped and resumed |
| Search | `python3 scripts/search.py "senior loan Tanzania"` | relevant emails on top, about 0.6 or higher |
| Estimate | `python3 scripts/generate.py --dry-run` | prints emails, calls and hours |
| One thread | `python3 scripts/generate.py --thread <id>` then `--show <id>` | fields correct against the real emails |
| Quality | fill `config/eval_set.json`, then `python3 scripts/eval.py --generate` | fact recall high, no forbidden facts |
| Logs | `python3 scripts/obs.py report` / `errors` / `dashboard` | every run has a status |

What to check in a thread's fields:

| Check | Correct when |
|---|---|
| English | all fields in English, also for French or German emails |
| No invention | every name, amount and date appears in the emails |
| History | one `[DD/MM/YYYY]` line per email, in order |
| Dates | forwards use the forwarded message's date; `(approx.)` only when unclear |
| NEW UPDATE! | only on emails that changed the deal; later emails copy the summary without it |
| Next Steps | real actions with the right party; done items kept with `✓` |
| Verification | anything unsupported is marked `needs_review` |

## 2.4 Look inside the database

The database is `proparco-data/db/proparco.db`.

- **Graphical:** install [DB Browser for SQLite](https://sqlitebrowser.org/dl/)
  (Linux: `sudo apt install sqlitebrowser`), open the file, **Browse Data**.
  Tables starting with `vec_` show "no such module: vec0"; that is normal.
  Close it before running the pipeline.

| Table | Holds |
|---|---|
| `email_results` | **the three fields per email**, review status, verification flags, Notion status |
| `thread_state` | running state of each thread, to continue when new emails arrive |
| `thread_summaries` | latest fields per thread, for search |
| `messages`, `chunks` | cleaned emails and their pieces |
| `meta` | which embedding model built the database |

## 2.5 Build a flash disk

```bash
python3 build_package.py --out /media/<you>/<disk>      # Linux
python build_package.py --out E:\                         # Windows (not yet tested)
```

This creates `proparco-drive` on the disk: launchers, scripts, the engine (Linux and `.exe`),
the Ministral model **extracted** from the `.llamafile` (without its unused 0.8 GB image module),
bge-m3, a portable Python 3.12 for Windows, sqlite-vec for both systems, and `SHA256SUMS.txt`.

- It needs internet once (Windows Python and sqlite-vec).
- It refuses to overwrite: delete the old `proparco-drive` on the disk first.
- `--with-data` also copies your data folder into `dist/`, for testing only. It contains
  client emails, so the script refuses `--with-data` together with `--out`: data is never
  written to a flash disk.

Check the copy, then test like a client (Part 1):

```bash
cd /media/<you>/<disk>/proparco-drive
sha256sum -c SHA256SUMS.txt --quiet && echo "All files OK"
sh start.sh
```

To measure the real start-up time, eject and re-plug the disk first; otherwise the models
are still cached in memory.

**What goes on a flash disk:** only the `proparco-drive` folder exactly as the build makes it
(about 2.9 GB): launchers, guides, `VERSION.txt`, `SHA256SUMS.txt`, `config/`, `scripts/`
(including both email extractors), `bin/` (the engine, Linux and `.exe`), `models/` (the two
GGUF files) and `python/` (portable Windows Python, sqlite-vec for both systems).

**Never on a flash disk:**

| Item | Why, and where it belongs |
|---|---|
| `proparco-data/` (emails, `proparco.db`, logs, `viewer.html`, dashboard) | Client data; created on each laptop at first run |
| Test data or earlier email exports | Real client emails; the developer's machine only |
| The `.llamafile` (3.4 GB) | Not needed; the package uses the extracted GGUF |
| `env/`, `.git/`, `dist/`, `__pycache__/` | Developer files |
| Tokens or passwords (e.g. Notion, later) | Secrets; never stored on a disk that travels |

**Check every disk before handing it over** (the `find` line must print nothing):

```bash
STICK=/media/<you>/<disk>
ls "$STICK"
find "$STICK" \( -name "*.db" -o -name "acquisition.json" -o -name "viewer.html" -o -name "*.llamafile" \) 2>/dev/null
cd "$STICK/proparco-drive" && sha256sum -c SHA256SUMS.txt --quiet && echo "All files OK"
cat VERSION.txt
```

**One build, many disks.** Build once, then copy `dist/proparco-drive` to each disk and check it.
Rebuild only after changing code, settings, guides or models; each rebuild writes a new
`VERSION.txt` (date, code commit, prompt version), so you can tell which build a disk carries.

## 2.6 Settings reference (`config/settings.json`)

| Setting | Meaning |
|---|---|
| `data_dir` | data folder (on the build: `~/proparco-data`) |
| `threads` | CPU threads for the models; match the physical cores |
| `api_key` | `"auto"`: new random key every run |
| `auto_approve` | `false`: results wait for review before Notion |
| `internal_domains` | your organisation's email domains |
| `generation.ctx_size` | model memory per request: 8192 tokens |
| `generation.batch_chars` | email text per model call: 16000 characters; long threads use several calls, nothing is cut |
| `generation.max_tokens` | maximum output per call: 1500 |
| `generation.include_quoted_history` | `"first_email"`: keep quoted earlier messages for forwards and a thread's first email |
| `generation.est_read_tok_s` / `est_write_tok_s` | speeds used by the time estimate; update after a health check |
| `quick_run_on_start` | `true`: on Windows, offer to process the emails selected in Outlook when the menu starts |
| `start_mode` | `"auto"`: `start.bat` runs the plug-and-play flow (set by the build for every package); `"menu"`: full menu (recommended in a developer folder) |
| `auto_source` | `"outlook"`: the automatic flow fetches from Outlook; `"none"`: only process emails already fetched |
| `auto_first_fetch_days` | how far back the very first automatic fetch goes (default 30) |
| `embedding.dimension` | 1024 for bge-m3; changing model means rebuilding the database |

## 2.7 Test results so far

| Date | Setup | Result |
|---|---|---|
| 25 Oct 2026 | i5-10310U, Linux, VM running | read 18.5 / write 3.2 tok/s |
| 26 Oct 2026 | same, VM off | read 32.3 / write 5.8 tok/s |
| 26 Oct 2026 | built flash disk, extracted GGUF | **read 42.7 / write 7.0 tok/s** |
| 26 Oct 2026 | bge-m3 cross-language check | FR-EN 0.937 vs unrelated 0.420: pass |
| 26 Oct 2026 | embedding 978 emails (1,344 chunks) | about 45 minutes, 0.5 chunks/s |
| 26 Oct 2026 | one email (NBC thread), prompt v3 | 79 s; all checks passed |
| 26 Oct 2026 | estimate, all 978 emails / 495 threads | about 15-20 hours: run overnight, or select threads |

## 2.8 Problems we met, and the fixes

| What you see | Cause | Fix |
|---|---|---|
| `data is not within the file bounds, model is corrupted or incomplete` | download stopped early | check size with `ls -lh`; download again with `-C -` |
| `the current context does not [support] logits computation` | text request sent to the embedding server | generation is port 8081, embeddings 8082 |
| `Exec format error` from Python | llamafile cannot be started directly by Python | already handled: the scripts start it through `sh` |
| `failed to read last 64kb of file: Is a directory` | `@` in a folder path confuses llamafile | already handled with relative paths; avoid `@` and spaces in deployment paths |
| `UNIQUE constraint failed: messages.fingerprint` | same email in two Outlook folders | already handled: duplicates are skipped |
| `No acquisition.json found` / everything counted as new | `data_dir` points to the wrong folder, or the database has another name | fix `data_dir`; the database must be `db/proparco.db` |
| Slow speeds | another program (e.g. a VM) using the CPU | close it; `vmstat 1` shows VM load in the `gu` column |
| Linux: `volume is dirty` when mounting the disk | disk unplugged from Windows without ejecting | `sudo ntfsfix -d /dev/sdX1`, or `chkdsk E: /f` on Windows |
| `git ... index.lock: File exists` | an earlier git command crashed | check no git is running, then `rm -f .git/index.lock` |
| A downloaded script contains another script's code | browser renamed a download (`file (1).py`) | check line 3 of each file names the file itself |

## 2.9 Security and privacy

- Models listen only on `127.0.0.1`, with a new API key each run and the sandbox on.
- Only the three generated fields will ever be sent to Notion; raw emails stay local.
- Logs and the dashboard hold timings, counts and IDs, never email content.
- **Never commit or share data:** `proparco-data/`, `dist/`, `*.db` and the models are in
  `.gitignore`. Share a data folder only encrypted, and only with people allowed to see it.
- On a managed work laptop, do not work around antivirus or USB rules. Get IT approval.

## 2.10 Windows and network notes

- The build provides `.exe` copies and a portable Python; nothing needs installing.
- Windows cannot run programs over 4 GB; the engine is small and the models are separate files.
- On the client network, only the Notion step needs the proxy:
  `HTTPS_PROXY=http://172.18.150.11:8080`. The launchers already set
  `NO_PROXY=127.0.0.1,localhost` so the local models never go through it.

## 2.11 Models and licences

| Component | Source | Licence |
|---|---|---|
| llamafile 0.10.6 (thin) | [github.com/mozilla-ai/llamafile](https://github.com/mozilla-ai/llamafile) | Apache 2.0 |
| Ministral 3 3B Instruct 2512, Q4_K_M | [mozilla-ai/llamafile_0.10](https://huggingface.co/mozilla-ai/llamafile_0.10) (model by [Mistral AI](https://huggingface.co/mistralai/Ministral-3-3B-Instruct-2512)) | Apache 2.0 |
| bge-m3, Q8_0 GGUF | [gpustack/bge-m3-GGUF](https://huggingface.co/gpustack/bge-m3-GGUF) (from [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3)) | MIT |
| sqlite-vec 0.1.6 | [github.com/asg017/sqlite-vec](https://github.com/asg017/sqlite-vec) | MIT / Apache 2.0 |
| Python 3.12 (Windows, embeddable) | [python.org](https://www.python.org/downloads/windows/) | PSF |

## 2.12 Resources

- [llamafile documentation](https://docs.mozilla.ai/llamafile/getting-started/quickstart)
- [Llamafile: bringing AI to the masses with fast CPU inference (Stephen Hood and Justine Tunney)](https://youtu.be/-mRi-B3t6fA)
- [DB Browser for SQLite](https://sqlitebrowser.org/)