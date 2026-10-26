# Proparco Drive: Setup Guide

This guide sets up the local AI engines for the Proparco email pipeline.
Everything runs on your own computer. No email content is sent to the internet.

When you finish this guide, you will have two local AI servers:
| Server | What it does | Model | Port |
|---|---|---|---|
| Generation | Writes the three Notion fields (Latest Deal Summary, Project History, Next Steps) | Ministral 3 3B (Q4_K_M) | 8081 |
| Embeddings | Turns email text into vectors for search | BAAI bge-m3 (Q8_0), 1024 dimensions | 8082 |

---

## 1. Before you start

You need:

- A Linux computer (Windows notes are at the end)
- `curl`, `python3` and `sha256sum` (already installed on most Linux systems)
- About **5 GB** of free disk space
- A stable internet connection for the downloads (one-time only)

**Tip:** close heavy programs and shut down any virtual machines while you test.
They compete for the CPU and make the AI much slower.

---

## 2. Create the project folders

```bash
mkdir -p ~/proparco-drive/{bin,models,scripts,config,python}
mkdir -p ~/proparco-data/{acquisitions,db,logs}
```

What each folder is for:

```
proparco-drive/        <- goes on the flash disk
├── bin/               the llamafile program
├── models/            the AI model files
├── scripts/           pipeline scripts (fetch, embed, generate, Notion)
├── config/            settings (model names, ports, Notion IDs)
└── python/            portable Python (added later)

proparco-data/         <- stays on the computer's own disk, NOT the flash disk
├── acquisitions/      extracted emails
├── db/                the search database (sqlite-vec)
└── logs/              run logs
```

**Why two separate folders?** If the flash disk is lost, it only contains
programs and public models. Client emails never sit on the flash disk.

---

## 3. Download the llamafile program

llamafile is one file that runs AI models on Windows, Linux and macOS
with nothing to install. We use the **thin** version: it has no GPU parts,
so it is smaller and works well on normal office laptops.

```bash
cd ~/proparco-drive/bin
curl -LO https://github.com/mozilla-ai/llamafile/releases/download/0.10.6/llamafile-0.10.6-thin
chmod +x llamafile-0.10.6-thin
```

---

## 4. Download the generation model (Ministral 3 3B)

1. Open the **Pre-built llamafiles** page in the llamafile documentation
   (link in Resources below).
2. Download **`Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile`** (about 3.4 GB).
3. Move it into the models folder and make it runnable:

```bash
mv ~/Downloads/Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile ~/proparco-drive/models/
chmod +x ~/proparco-drive/models/Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile
```

**Why this model?** It is under 4 GB (Windows cannot run programs larger than 4 GB),
it is from Mistral (strong in French, German, Spanish and Italian), and it is
small enough to run on a CPU without a graphics card.

---

## 5. Download the embedding model (bge-m3)

BAAI publishes bge-m3 only in PyTorch format. llamafile needs **GGUF** format,
so we use the GGUF version published by GPUStack, which is converted from the
official BAAI model.

```bash
cd ~/proparco-drive
curl -L -C - --retry 5 --retry-delay 5 -o models/bge-m3-q8.gguf \
  "https://huggingface.co/gpustack/bge-m3-GGUF/resolve/main/bge-m3-Q8_0.gguf"
```

`-C -` resumes the download if it stops halfway. `--retry 5` tries again if the connection drops.

### Check the download is complete

```bash
ls -lh models/bge-m3-q8.gguf
sha256sum models/bge-m3-q8.gguf
```

- The size should be about **606M**. (Linux shows MiB; Hugging Face shows 635 MB. These are the same size.)
- If the file is only a few MB, the download stopped early. Run the download command again.
- Compare the SHA256 with the one shown on the file's Hugging Face page.

Recorded SHA256 (25 Oct 2026):

```
950f4a8e5e19477a6d3c26d2f162233c20002c601f75e4b002e3239997821167  bge-m3-q8.gguf
```

---

## 6. Start the servers

Each server runs in its **own terminal window** and keeps running until you stop it.
That is normal. To stop a server, press **Ctrl+C** in its window.

**Important:** a terminal running a server does not accept commands.
Run all tests in a **separate** terminal (Ctrl+Shift+T opens a new tab).

### Terminal 1: generation server

```bash
cd ~/proparco-drive
./models/Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile --server \
  --host 127.0.0.1 --port 8081 -t 4 -c 4096 -np 1 --gpu disable
```

### Terminal 2: embedding server

```bash
cd ~/proparco-drive
./bin/llamafile-0.10.6-thin --server --embedding \
  -m models/bge-m3-q8.gguf --host 127.0.0.1 --port 8082 -t 4 -np 1 --gpu disable
```

Each server is ready when its log shows:

```
listening on http://127.0.0.1:808X
```

### What the options mean

| Option | Meaning |
|---|---|
| `--server` | Run as a local server (no chat window) |
| `--embedding` | Embeddings only (used for bge-m3) |
| `-m <file>` | Which model file to load |
| `--host 127.0.0.1` | Only this computer can connect. Never use `0.0.0.0` |
| `--port` | 8081 for generation, 8082 for embeddings |
| `-t 4` | Use 4 CPU threads (match the number of physical cores) |
| `-c 4096` | Maximum text length per request, in tokens |
| `-np 1` | Handle one request at a time (saves memory) |
| `--gpu disable` | Use the CPU only. This also turns on llamafile's security sandbox |

---

## 7. Test the servers

Run these in a **third** terminal while both servers are running.

### Test 1: generation speed

```bash
curl -s http://127.0.0.1:8081/completion \
  -d '{"prompt":"Summarise in one sentence: The board approved the loan to the solar project in Kenya, pending due diligence.","n_predict":80}' \
  | python3 -c "import sys,json;t=json.load(sys.stdin)['timings'];print('read speed:',round(t['prompt_per_second'],1),'tok/s | write speed:',round(t['predicted_per_second'],1),'tok/s')"
```

### Test 2: embedding size

```bash
curl -s http://127.0.0.1:8082/v1/embeddings -H "Content-Type: application/json" \
  -d '{"input":"Le comité a approuvé le prêt."}' \
  | python3 -c "import sys,json;print(len(json.load(sys.stdin)['data'][0]['embedding']))"
```

Expected result: **`1024`**

### Test 3: cross-language quality

This checks that the embedding model understands that French and English
sentences with the same meaning are similar.

```bash
python3 - <<'EOF'
import json, urllib.request, math
def emb(t):
    r = urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8082/v1/embeddings",
        data=json.dumps({"input": t}).encode(), headers={"Content-Type": "application/json"}))
    return json.load(r)["data"][0]["embedding"]
def cos(a, b):
    return sum(x*y for x, y in zip(a, b)) / (math.sqrt(sum(x*x for x in a)) * math.sqrt(sum(y*y for y in b)))
fr = emb("Le comité a approuvé le prêt au projet solaire.")
en = emb("The committee approved the loan to the solar project.")
xx = emb("My cat likes to sleep on the sofa.")
print("FR vs EN (same meaning):", round(cos(fr, en), 3))
print("FR vs unrelated:       ", round(cos(fr, xx), 3))
EOF
```

Expected result: the **FR vs EN** score is clearly higher than the **unrelated** score.

---

## 8. Test results

| Date | Machine | Test | Result |
|---|---|---|---|
| 25 Oct 2026 | i5-10310U, Linux, VM running | Generation read / write speed | 18.5 / 3.2 tok/s |
| 25 Oct 2026 | i5-10310U, Linux | Embedding size | 1024 ✓ |
| | | Cross-language quality | _to fill in_ |
| | i5-10310U, Linux, VM **off** | Generation read / write speed | _to fill in_ |

The first speed result was measured with a virtual machine using part of the CPU,
so it is lower than the real speed of this laptop.

---

## 9. Problems and fixes

| What you see | What it means | Fix |
|---|---|---|
| `data is not within the file bounds, model is corrupted or incomplete` | The model download stopped early | Check the size with `ls -lh`, then download again |
| `the current context does not [support] logits computation` | A text-generation request was sent to the embedding server | Send generation requests to port **8081**, embeddings to **8082** |
| A command typed in a server window does nothing | Server windows do not accept commands | Use a separate terminal |
| `No such file or directory` | Wrong folder or wrong file name | Run `pwd` and `ls bin/ models/` to check |
| `reserving 802.50 MiB extra for mmproj model` | Ministral loads its image module, even with `--no-mmproj` | Harmless but uses RAM. Fix later by using a plain GGUF with the thin binary |
| `CORS is set to allow all origins ... no API key` | Any browser page on this computer could call the server | Add `--api-key` before client deployment (see below) |
| Very slow speeds | Something else is using the CPU | Close the VM and heavy apps. Check with `vmstat 1` (the `gu` column shows VM usage) |

---

## 10. Security notes

- Servers only listen on `127.0.0.1`, so other computers cannot reach them.
- `--gpu disable` turns on llamafile's sandbox (log shows `sandbox: pledge(...) active`).
- Client email data stays in `proparco-data/`, not on the flash disk.
- **Before client deployment:** add `--api-key <random-string>` to both servers
  and send that key from the scripts.

---

## 11. Windows notes (for the client laptop)

- Rename the files to end in `.exe`, for example `llamafile-0.10.6-thin.exe`.
- Windows cannot run programs larger than 4 GB. Both models are under this limit.
- Corporate antivirus may block programs run from a USB drive. Test this early on the real laptop.
- On the client network, set the proxy before the Notion step only:
  `HTTPS_PROXY=http://172.18.150.11:8080` and `NO_PROXY=127.0.0.1,localhost`.

---

## 12. Models and licences

| Component | Source | Licence |
|---|---|---|
| llamafile 0.10.6 (thin) | github.com/mozilla-ai/llamafile | Apache 2.0 |
| Ministral 3 3B Instruct 2512 Q4_K_M | llamafile pre-built models | Apache 2.0 |
| bge-m3 Q8_0 GGUF | huggingface.co/gpustack/bge-m3-GGUF (from BAAI/bge-m3) | MIT |
---

## Resources

- [llamafile Documentation](https://docs.mozilla.ai/llamafile/getting-started/quickstart)
- [llamafile on GitHub](https://github.com/mozilla-ai/llamafile)
- [Llamafile: bringing AI to the masses with fast CPU inference (Stephen Hood and Justine Tunney)](https://youtu.be/-mRi-B3t6fA)
- [BAAI bge-m3 (original model)](https://huggingface.co/BAAI/bge-m3)
- [GPUStack bge-m3 GGUF](https://huggingface.co/gpustack/bge-m3-GGUF)