# Proparco Drive: Windows setup for builders (PowerShell)

How to recreate the Proparco email assistant on a Windows machine, run it, watch its logs,
look inside its database, and build a flash disk for clients.

Two local AI models do the work. The scripts start and stop them automatically; you never
run them by hand (except in section 10, for debugging).

| Model | What it does | File | Port |
| --- | --- | --- | --- |
| Ministral 3 3B (Q4_K_M) | Writes Latest Deal Summary, Project History and Next Steps | `Ministral-3-3B-Instruct-2512-Q4_K_M.gguf` | 8081 |
| BAAI bge-m3 (Q8_0) | Turns email text into vectors for search (1024 dimensions) | `bge-m3-q8.gguf` | 8082 |

Everything runs on your own computer. No email content is sent to the internet.

> Status: the Linux version is fully tested. These Windows steps are being verified;
> note anything that does not work as written.

---

## 1. Before you start

- Windows 10 or 11, 64-bit, with 8 GB of memory (16 GB is better).
- [Git for Windows](https://git-scm.com/download/win).
- [Python 3.12](https://www.python.org/downloads/windows/) from python.org. During install,
  tick **"Add python.exe to PATH"**.
- About 8 GB of free disk space and a stable internet connection (one-time downloads).
- Use **PowerShell** for every command below.
- In PowerShell, always type **`curl.exe`**: plain `curl` is an alias for something else.
- Close heavy programs and virtual machines while testing. They slow the AI down a lot.

Avoid `@` characters and spaces in the folders you use. The examples use your home folder
(`$HOME`, i.e. `C:\Users\<you>`).

---

## 2. Get the code

You can clone onto the laptop's own disk or **directly onto a flash disk** and work from there.
Either way, the data always stays on the laptop (section 6).

**Option A: on the laptop**

```powershell
cd $HOME
git clone <repo-url> proparco
cd $HOME\proparco\proparco-drive
```

**Option B: on a flash disk** (format it NTFS or exFAT; the example uses drive `E:`)

```powershell
cd E:\
git clone <repo-url> proparco
cd E:\proparco\proparco-drive
```

If Git stops with *"detected dubious ownership in repository"*, the flash disk does not record
file owners. Allow that one folder, then repeat the command:

```powershell
git config --global --add safe.directory E:/proparco
```

The drive letter can change when the disk is plugged in elsewhere (`E:` on one machine, `F:` on
another). Nothing in the project depends on it, except the Python environment, which is why
section 3 keeps that on the laptop.

From now on, run every command from this `proparco-drive` folder unless stated otherwise.

## 3. Python environment

Create it **on the laptop**, in your home folder, even when the code is on a flash disk.
A Python environment remembers its full path including the drive letter, so one stored on a
flash disk breaks as soon as the disk gets a different letter. Each laptop gets its own
environment; it takes a minute to create.

```powershell
python -m venv $HOME\proparco-env
& $HOME\proparco-env\Scripts\python.exe -m pip install sqlite-vec
```

To keep commands short, store the environment's Python in a variable for this session:

```powershell
$py = "$HOME\proparco-env\Scripts\python.exe"
```

Repeat that line whenever you open a new PowerShell window. (You can also activate the
environment with `$HOME\proparco-env\Scripts\Activate.ps1`, but Windows often blocks that
script; the variable avoids the problem.)

**Check that Python can load the search extension.** This is the step most likely to differ on
Windows:

```powershell
& $py -c "import sqlite3, sqlite_vec; db = sqlite3.connect(':memory:'); db.enable_load_extension(True); sqlite_vec.load(db); print('sqlite-vec', db.execute('select vec_version()').fetchone()[0], 'OK')"
```

It should print something like `sqlite-vec v0.1.6 OK`. If it fails, stop and report the message.

## 4. Download the AI files

```powershell
curl.exe -L -o bin\llamafile-0.10.6-thin.exe https://github.com/mozilla-ai/llamafile/releases/download/0.10.6/llamafile-0.10.6-thin
curl.exe -L -C - --retry 5 -o models\Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile https://huggingface.co/mozilla-ai/llamafile_0.10/resolve/main/Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile
curl.exe -L -C - --retry 5 -o models\bge-m3-q8.gguf https://huggingface.co/gpustack/bge-m3-GGUF/resolve/main/bge-m3-Q8_0.gguf
```

- The engine is saved with `.exe` at the end, because Windows only runs programs with that ending.
- `-C -` resumes a download that stopped; `--retry 5` retries if the connection drops.
  If a download stops early, run the same line again.

### Take the Ministral model out of its package

The `.llamafile` is a program with the model packed inside. The scripts use the plain model
file, which avoids a 3.4 GB `.exe` and leaves out an unused 0.8 GB image module:

```powershell
& $py -c "import build_package as b; from pathlib import Path; b.extract_ministral(Path('models/Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile'), Path('models/Ministral-3-3B-Instruct-2512-Q4_K_M.gguf'))"
```

### Check the downloads

```powershell
Get-ChildItem bin, models | Select-Object Name, @{n="MB";e={[math]::Round($_.Length/1MB)}}
(Get-FileHash models\bge-m3-q8.gguf -Algorithm SHA256).Hash -ieq "950f4a8e5e19477a6d3c26d2f162233c20002c601f75e4b002e3239997821167"
```

| File | About |
| --- | --- |
| `llamafile-0.10.6-thin.exe` | a few tens of MB |
| `Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile` | 3,200 MB |
| `Ministral-3-3B-Instruct-2512-Q4_K_M.gguf` | 2,050 MB |
| `bge-m3-q8.gguf` | 606 MB |

The hash line should print `True`. A file of only a few MB means the download failed.
Once the `.gguf` exists, the `.llamafile` is no longer needed; you may delete it to save space.

## 5. Settings

Open `config\settings.json` in a text editor and set:

```json
"data_dir": "~/proparco-data",
"internal_domains": ["proparco.fr", "afd.fr"],
```

and in the `"generation"` section:

```json
"binary": "bin/llamafile-0.10.6-thin",
"model": "models/Ministral-3-3B-Instruct-2512-Q4_K_M.gguf",
```

Keep the forward slashes and leave out `.exe`; the scripts add it on Windows.
`~/proparco-data` becomes `C:\Users\<you>\proparco-data`.

## 6. Data

Create the data folder:

```powershell
"acquisitions","db","logs" | ForEach-Object { New-Item -ItemType Directory -Force "$HOME\proparco-data\$_" }
```

Then either:

- copy an existing data folder from the project lead into `$HOME\proparco-data`
  (it contains client emails: only if you are allowed to see them), or
- put new acquisition run folders (each containing `acquisition.json`) into
  `$HOME\proparco-data\acquisitions`.

## 7. Run it

Set two things for this PowerShell window, then start the menu:

```powershell
$env:PYTHONUTF8 = "1"
$env:NO_PROXY = "127.0.0.1,localhost"
& $py scripts\menu.py
```

`NO_PROXY` makes sure the local models are never reached through a company proxy.

First, choose **1 (Health check)**. It should end with `Embedding check: PASS` and two speeds.
Windows may then ask about the firewall for `llamafile`: click **Cancel**. The models only
listen on this computer and work without network access.

| Menu | What it does |
| --- | --- |
| 1 | Health check |
| 2 / 3 | Embed new emails: dry run / for real (about 45 minutes for 1,000 emails) |
| 4 | List email threads |
| 5 | Estimate generation time |
| 6 | Generate the three fields for one thread |
| 7 | Generate for all new emails (many hours; overnight) |
| 8 | Show the fields for a thread |
| 9 | Search by meaning, across languages |
| 10 / 11 | Run report / recent errors |
| 12 | Log dashboard in the browser |
| 13 | Quality evaluation |
| 14 | Browse threads in the browser (viewer) |
| 15 | Fetch emails from Outlook or Gmail, then embed |

Every script can also be run directly, for example `& $py scripts\generate.py --list`.

## 8. See the logs

Every run is logged in `$HOME\proparco-data\logs`. The logs hold timings, token counts,
IDs and error messages only, never email content.

| What | How |
| --- | --- |
| Table of recent runs | menu **10**, or `& $py scripts\obs.py report` |
| Errors and warnings, with details | menu **11**, or `& $py scripts\obs.py errors` |
| Charts in the browser | menu **12**, or `& $py scripts\obs.py dashboard` |
| Watch a run live | open a second PowerShell window: `Get-Content $HOME\proparco-data\logs\pipeline.log -Tail 20 -Wait` |
| The model servers' own output | `$HOME\proparco-data\logs\generation.log` and `embedding.log` |

The dashboard (`dashboard.html`) shows a timeline of runs, model speed per call, errors,
emails held for review, and quality scores. It works offline and is safe to share.

## 9. Look inside the database

1. Install [DB Browser for SQLite](https://sqlitebrowser.org/dl/) (the "Standard installer"
   or the portable version).
2. Open `C:\Users\<you>\proparco-data\db\proparco.db`.
3. **Browse Data** → table **`email_results`**: one row per email with
   `latest_deal_summary`, `project_history`, `next_steps`, `review_status` and
   `verification`.

Tables starting with `vec_` show "no such module: vec0". That is normal: they only hold search
vectors. Close DB Browser before running the menu again.

## 10. Advanced: start the servers by hand (debugging only)

Useful to test the engine on its own. Stop the menu first. Run each server in its own tab
(Ctrl+Shift+T in Windows Terminal); a tab running a server does not accept commands.

**Tab 1: generation**

```powershell
.\bin\llamafile-0.10.6-thin.exe --server -m models\Ministral-3-3B-Instruct-2512-Q4_K_M.gguf --host 127.0.0.1 --port 8081 -t 4 -c 8192 -np 1 --gpu disable
```

**Tab 2: embeddings**

```powershell
.\bin\llamafile-0.10.6-thin.exe --server --embedding -m models\bge-m3-q8.gguf --host 127.0.0.1 --port 8082 -t 4 -np 1 --gpu disable -b 1024 -ub 1024
```

Ready when the log shows `listening on http://127.0.0.1:808X`.

**Tab 3: tests**

```powershell
# Generation speed
$body = '{"prompt":"Summarise in one sentence: The board approved the loan to the solar project in Kenya, pending due diligence.","n_predict":80}'
$r = Invoke-RestMethod -Uri http://127.0.0.1:8081/completion -Method Post -ContentType "application/json" -Body ([Text.Encoding]::UTF8.GetBytes($body))
"read speed: {0:N1} tok/s | write speed: {1:N1} tok/s" -f $r.timings.prompt_per_second, $r.timings.predicted_per_second

# Embedding size (expect 1024)
$body = '{"input":"Le comité a approuvé le prêt."}'
$r = Invoke-RestMethod -Uri http://127.0.0.1:8082/v1/embeddings -Method Post -ContentType "application/json" -Body ([Text.Encoding]::UTF8.GetBytes($body))
$r.data[0].embedding.Count
```

The explicit UTF-8 encoding keeps accented characters such as `é` intact. Press Ctrl+C in
tabs 1 and 2 when done. The menu's health check runs all of these tests for you.

## 11. Build a flash disk for clients

Plug in the flash disk (format it **NTFS** or **exFAT**) and note its letter, e.g. `E:`.

```powershell
& $py build_package.py --out E:\
```

This creates `E:\proparco-drive` with launchers, scripts, both models, a portable Python
for Windows and `SHA256SUMS.txt`. It needs internet once. It never copies client data to the
disk: data is always copied to the laptop separately.

Check the copy:

```powershell
cd E:\proparco-drive
$bad = Get-Content SHA256SUMS.txt | ForEach-Object {
  $hash, $file = $_ -split '  ', 2
  if ((Get-FileHash $file -Algorithm SHA256).Hash -ne $hash.ToUpper()) { $file } }
if ($bad) { "Changed or missing: $bad" } else { "All files OK" }
```

Then test it the way a client will: double-click `E:\proparco-drive\start.bat`
(see `SETUP.md`, Part 1). Eject the disk properly before unplugging it.

## 12. Windows troubleshooting

| Problem | Fix |
| --- | --- |
| `curl` behaves oddly | Use `curl.exe`, not `curl` |
| Git: "detected dubious ownership" (code on a flash disk) | `git config --global --add safe.directory E:/proparco` (use your drive letter) |
| Python environment stopped working after moving the flash disk | The drive letter changed. Keep the environment on the laptop (section 3); recreate it there if needed |
| "running scripts is disabled" | Use `& $py ...` as shown instead of activating the environment |
| sqlite-vec check (section 3) fails | Report the message; the Python build may not allow extensions |
| Antivirus quarantines `llamafile-0.10.6-thin.exe` | On your own test machine you may restore it and add an exclusion for the `bin` folder. **On a work or client laptop, never do this: ask IT.** |
| A downloaded file is blocked ("Windows protected your PC") | Files downloaded with a browser carry a block; `Unblock-File bin\llamafile-0.10.6-thin.exe` on your own machine. Downloads with `curl.exe` are not affected |
| Port 8081 or 8082 is in use | `Get-NetTCPConnection -LocalPort 8081` shows the program; close it or change the port in `settings.json` |
| `No acquisition.json found` / no threads | Check `data_dir` in `settings.json` and the path shown at the top of the menu |
| Slow generation | Close heavy programs and VMs; set `"threads"` to your number of physical cores |
| Strange symbols instead of ✓ | Display only; run `$env:PYTHONUTF8 = "1"` before starting |
| Anything else | Menu **11** shows the error and where it happened |