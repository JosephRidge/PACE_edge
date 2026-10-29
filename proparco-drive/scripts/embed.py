"""
embed.py - clean, chunk, embed and store acquired emails in sqlite-vec.

Run from the proparco-drive folder:
    python3 scripts/embed.py --dry-run SOURCE          # cleaning report only, no AI
    python3 scripts/embed.py --dry-run --show 3 SOURCE # also print 3 cleaned emails
    python3 scripts/embed.py --limit 20 SOURCE         # embed the first 20 new emails
    python3 scripts/embed.py SOURCE                    # embed all new emails

SOURCE is a run folder (containing acquisition.json) or a folder of run folders.
Default SOURCE: <data_dir>/acquisitions

Safe to stop and re-run: emails already in the database are skipped.
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import Engines, load_config  # noqa: E402
import obs  # noqa: E402

CHUNK_CHARS = 1400          # ~400 tokens; must stay under the server's 1024-token batch
OVERLAP_CHARS = 200         # repeated text between neighbouring chunks
MAX_CHUNKS_PER_EMAIL = 8    # stops newsletters and long reports dominating the index
COMMIT_EVERY = 10           # emails per database commit


# ================================================================ cleaning

# Start of a quoted earlier email (English, French, German, Spanish, Italian)
SEPARATOR = re.compile(
    r"^\s*(-{2,}\s*(original message|message d'origine|ursprüngliche nachricht|"
    r"mensaje original|messaggio originale)\s*-{2,}|_{10,})\s*$", re.I)
REPLY_INTRO = re.compile(
    r"^\s*(on .{5,200} wrote:|le .{5,200} a [ée]crit\s*:|am .{5,200} schrieb.{0,100}:|"
    r"el .{5,200} escribi[óo]\s*:|il .{5,200} ha scritto\s*:)\s*$", re.I)
HEADER_FROM = re.compile(r"^\s*(from|de|von|da)\s*:", re.I)
HEADER_SENT = re.compile(
    r"^\s*(sent|envoy[ée]|gesendet|enviado|inviato|date|datum|fecha|data)\s*:", re.I)
HEADER_ANY = re.compile(
    r"^\s*(from|de|von|da|sent|envoy[ée]|gesendet|enviado|inviato|date|datum|fecha|data|"
    r"to|[àa]|an|para|a|cc|subject|objet|betreff|asunto|oggetto)\s*:", re.I)

SIGNOFF = re.compile(
    r"^\s*(best regards|kind regards|warm regards|regards|best|many thanks|thanks and regards|"
    r"cordialement|bien cordialement|bien à vous|salutations|"
    r"mit freundlichen gr(ü|ue)(ß|ss)en|viele gr(ü|ue)(ß|ss)e|beste gr(ü|ue)(ß|ss)e|"
    r"saludos( cordiales)?|un saludo|atentamente|cordiali saluti|distinti saluti|saluti)"
    r"\s*[,.!]?\s*$", re.I)
DISCLAIMER = re.compile(
    r"(this (e-?mail|message|communication)( and any attachments?)? (is|are|may be|contains?) "
    r"(confidential|intended|privileged)|"
    r"ce (message|courriel)( et (toutes )?(les |ses )?pi[èe]ces jointes)? (est|sont|peut|peuvent)|"
    r"avant d'imprimer|please consider the environment|think before you print|"
    r"diese e-?mail (enth[äa]lt|ist|kann)|"
    r"este (correo|mensaje)( electr[óo]nico)?( y sus anexos)? (es|son|puede|contiene)|"
    r"questo messaggio( e i suoi allegati)? (è|e|pu[òo]|contiene))", re.I)
NOISE = [
    (re.compile(r"\[cid:[^\]]*\]", re.I), ""),
    (re.compile(r"<mailto:[^>]*>", re.I), ""),
    (re.compile(r"<https?://[^>\s]*>"), ""),
    (re.compile(r"[ \t\u00a0]+"), " "),
]


def _quote_start(lines):
    """Index of the first line of quoted earlier mail, or None."""
    for i, line in enumerate(lines):
        if SEPARATOR.match(line) or REPLY_INTRO.match(line) or line.lstrip().startswith(">"):
            return i
        if HEADER_FROM.match(line) and any(HEADER_SENT.match(l) for l in lines[i + 1:i + 5]):
            return i
    return None


def _drop_header_block(lines):
    """Remove separator and From/Sent/To/Subject lines at the top of a quoted email."""
    i = 0
    while i < len(lines) and (not lines[i].strip() or SEPARATOR.match(lines[i])
                              or HEADER_ANY.match(lines[i])):
        i += 1
    return lines[i:]


def clean_body(text, depth=0):
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # 1. Keep only the new part of the email, not the quoted history.
    q = _quote_start(lines)
    if q is not None:
        new_part = "\n".join(lines[:q]).strip()
        if len(new_part) < 40 and depth == 0:
            # A forward with little or no new text: keep the forwarded email instead.
            return clean_body("\n".join(_drop_header_block(lines[q:])), depth=1)
        lines = lines[:q]

    # 2. Cut at the sign-off ("Best regards", "Cordialement", ...) in the lower part.
    for i in range(int(len(lines) * 0.3), len(lines)):
        if SIGNOFF.match(lines[i]):
            lines = lines[:i]
            break

    text = "\n".join(lines)

    # 3. Remove disclaimer paragraphs.
    paras = re.split(r"\n\s*\n", text)
    text = "\n\n".join(p for p in paras if not DISCLAIMER.search(p))

    # 4. Remove image placeholders, mailto/URL clutter and extra spaces.
    for pattern, repl in NOISE:
        text = pattern.sub(repl, text)
    text = "\n".join(l.strip() for l in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text, size=CHUNK_CHARS, overlap=OVERLAP_CHARS):
    if len(text) <= size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):  # break at a line end or space, not mid-word
            brk = text.rfind("\n", start + size // 2, end)
            if brk == -1:
                brk = text.rfind(" ", start + size // 2, end)
            if brk != -1:
                end = brk
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
        space = text.find(" ", start, end)
        if space != -1:
            start = space + 1
    return chunks


# ================================================================ reading acquisitions

EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


def as_list(value):
    """PowerShell turns a one-item list into a single object; undo that."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def extract_email(s):
    m = EMAIL_RE.search(s or "")
    return m.group(0).lower() if m else None


def parse_time(s):
    if not s:
        return None
    m = re.search(r"/Date\((-?\d+)", s)  # Windows PowerShell 5.1 date format
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).isoformat()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d-%m-%Y %H:%M:%S",
                "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).isoformat()
        except ValueError:
            continue
    return None


def find_runs(source):
    source = Path(source)
    if source.is_file():
        return [source.parent]
    return sorted(p.parent for p in source.rglob("acquisition.json"))


def load_messages(run_dir):
    with open(run_dir / "acquisition.json", encoding="utf-8-sig") as f:
        data = json.load(f)
    for m in as_list(data.get("messages")):
        sender = m.get("sender") or ""
        sender_email = extract_email(sender)
        recipients = [str(r) for r in as_list(m.get("to")) + as_list(m.get("cc"))]
        attachments = [a.get("filename", "") for a in as_list(m.get("attachments"))
                       if isinstance(a, dict)]
        received_raw = m.get("receivedTime") or ""
        yield {
            "entry_id": m.get("entryId") or "",
            "conversation_id": m.get("conversationId") or "",
            "subject": (m.get("subject") or "").strip(),
            "sender": sender,
            "sender_email": sender_email,
            "sender_domain": sender_email.split("@")[1] if sender_email else None,
            "recipients": recipients,
            "received_raw": received_raw,
            "received_iso": parse_time(received_raw),
            "attachments": attachments,
            "body": m.get("body") or "",
            "run_folder": run_dir.name,
            "fingerprint": hashlib.sha1(
                f"{sender}|{received_raw}|{m.get('subject')}".encode("utf-8")).hexdigest(),
        }


# ================================================================ database

def open_db(path, cfg):
    try:
        import sqlite_vec
    except ImportError:
        sys.exit("sqlite-vec is not installed. Run: pip install sqlite-vec")

    dim = cfg["embedding"]["dimension"]
    model = Path(cfg["embedding"]["model"]).name
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)

    db.executescript(f"""
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS messages (
            entry_id        TEXT PRIMARY KEY,
            fingerprint     TEXT UNIQUE,
            conversation_id TEXT,
            subject         TEXT,
            sender          TEXT,
            sender_email    TEXT,
            sender_domain   TEXT,
            recipients      TEXT,
            received_iso    TEXT,
            received_raw    TEXT,
            attachments     TEXT,
            body_clean      TEXT,
            run_folder      TEXT,
            added_at        TEXT
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id          INTEGER PRIMARY KEY,
            entry_id    TEXT NOT NULL REFERENCES messages(entry_id),
            chunk_index INTEGER,
            text        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_entry ON chunks(entry_id);
        CREATE INDEX IF NOT EXISTS idx_msg_conv     ON messages(conversation_id);
        CREATE INDEX IF NOT EXISTS idx_msg_domain   ON messages(sender_domain);
        CREATE INDEX IF NOT EXISTS idx_msg_time     ON messages(received_iso);
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks
            USING vec0(embedding float[{dim}] distance_metric=cosine);
    """)

    # Never mix vectors from two different embedding models in one database.
    stored = dict(db.execute("SELECT key, value FROM meta").fetchall())
    if not stored:
        db.executemany("INSERT INTO meta VALUES (?, ?)",
                       [("embedding_model", model), ("dimension", str(dim))])
        db.commit()
    elif stored.get("embedding_model") != model or stored.get("dimension") != str(dim):
        sys.exit(f"This database was built with {stored.get('embedding_model')} "
                 f"({stored.get('dimension')} dims), but config uses {model} ({dim} dims).\n"
                 f"Delete {path} and run embed.py again to rebuild it.")
    return db, sqlite_vec


def already_stored(db, msg):
    return db.execute("SELECT 1 FROM messages WHERE entry_id = ? OR fingerprint = ?",
                      (msg["entry_id"], msg["fingerprint"])).fetchone() is not None


# ================================================================ main

def main():
    ap = argparse.ArgumentParser(description="Clean, chunk, embed and store acquired emails.")
    ap.add_argument("source", nargs="?", help="run folder or folder of run folders")
    ap.add_argument("--dry-run", action="store_true", help="report only; no AI, no database writes")
    ap.add_argument("--show", type=int, default=0, help="print N cleaned emails (with --dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="only process the first N new emails")
    args = ap.parse_args()

    cfg = load_config()
    ob = obs.Observer("embed", cfg["_data_dir"] / "logs", vars(args))
    source = Path(args.source) if args.source else cfg["_data_dir"] / "acquisitions"
    runs = find_runs(source)
    if not runs:
        sys.exit(f"No acquisition.json found under {source}")

    db_path = cfg["_data_dir"] / "db" / "proparco.db"
    # A dry run still reads an existing database, so "already in database" is accurate.
    if args.dry_run and not db_path.exists():
        db, sqlite_vec = None, None
    else:
        db, sqlite_vec = open_db(db_path, cfg)

    # ---- collect and clean new messages
    stats = dict(total=0, stored=0, duplicate=0, empty=0, no_email=0, chars_in=0, chars_out=0)
    seen, todo = set(), []
    for run in runs:
        for msg in load_messages(run):
            stats["total"] += 1
            # The same email can appear twice with different Outlook IDs, e.g. when it
            # sits in both searched folders. Catch both kinds of duplicate.
            keys = {k for k in (msg["entry_id"], msg["fingerprint"]) if k}
            if keys & seen:
                stats["duplicate"] += 1
                continue
            seen |= keys
            if db and already_stored(db, msg):
                stats["stored"] += 1
                continue
            msg["body_clean"] = clean_body(msg["body"])
            stats["chars_in"] += len(msg["body"])
            stats["chars_out"] += len(msg["body_clean"])
            if not msg["sender_email"]:
                stats["no_email"] += 1
            if not msg["body_clean"]:
                stats["empty"] += 1
                continue
            msg["chunks"] = chunk_text(msg["body_clean"])[:MAX_CHUNKS_PER_EMAIL]
            todo.append(msg)

    if args.limit:
        todo = todo[:args.limit]
    n_chunks = sum(len(m["chunks"]) for m in todo)
    n_tokens = sum(len(c) for m in todo for c in m["chunks"]) // 4
    saved = 100 * (1 - stats["chars_out"] / stats["chars_in"]) if stats["chars_in"] else 0

    print(f"Run folders:              {len(runs)}")
    print(f"Emails found:             {stats['total']}")
    print(f"  already in database:    {stats['stored']}")
    print(f"  duplicates in source:   {stats['duplicate']}")
    print(f"  empty after cleaning:   {stats['empty']}")
    print(f"  to process now:         {len(todo)}")
    print(f"Text removed by cleaning: {saved:.0f}%  "
          f"({stats['chars_in']:,} -> {stats['chars_out']:,} characters)")
    print(f"Chunks to embed:          {n_chunks}  (~{n_tokens:,} tokens)")
    ob.event("plan", run_folders=len(runs), emails_found=stats["total"],
             already_stored=stats["stored"], duplicates=stats["duplicate"],
             empty_after_cleaning=stats["empty"], to_process=len(todo), chunks=n_chunks,
             est_tokens=n_tokens, cleaning_removed_pct=round(saved, 1),
             senders_without_email=stats["no_email"])
    if stats["no_email"]:
        ob.warn("senders without SMTP address", count=stats["no_email"])
    if stats["no_email"]:
        print(f"WARNING: {stats['no_email']} senders have no email address "
              f"(Exchange internal format). Counterparty matching will miss these.")

    if args.dry_run:
        for m in todo[:args.show]:
            print("\n" + "=" * 70)
            print(f"Subject: {m['subject']}\nFrom: {m['sender']}\nDate: {m['received_iso']}")
            print(f"Chunks: {len(m['chunks'])}  |  {len(m['body'])} -> {len(m['body_clean'])} chars")
            print("-" * 70 + "\n" + m["body_clean"][:1500])
        print("\nDry run: nothing was embedded or saved.")
        ob.finish("dry_run")
        return

    if not todo:
        print("Nothing new to embed.")
        ob.finish("nothing_to_do")
        return

    # ---- embed and store
    prefix = cfg["embedding"].get("doc_prefix", "")
    started, done_chunks = time.time(), 0
    with Engines(cfg, only=["embedding"], obs=ob) as eng:
        for i, m in enumerate(todo, 1):
            header = f"Subject: {m['subject']}\nFrom: {m['sender']}\n\n"
            email_id = obs.short_id(m["entry_id"] or m["fingerprint"])
            t0 = time.time()
            with ob.stage("embed_email", email=email_id):
                vectors, usage = eng.embed([prefix + header + c for c in m["chunks"]],
                                           return_usage=True)
            ob.model_call("embedding", "embed_email", time.time() - t0, usage=usage,
                          email=email_id, chunks=len(m["chunks"]))

            cur = db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (m["entry_id"] or m["fingerprint"], m["fingerprint"], m["conversation_id"],
                 m["subject"], m["sender"], m["sender_email"], m["sender_domain"],
                 json.dumps(m["recipients"], ensure_ascii=False), m["received_iso"],
                 m["received_raw"], json.dumps(m["attachments"], ensure_ascii=False),
                 m["body_clean"], m["run_folder"], datetime.now(timezone.utc).isoformat()))
            if cur.rowcount == 0:
                ob.warn("duplicate skipped at insert", email=email_id)
            if cur.rowcount == 1:  # 0 means already stored under another ID: keep the first copy
                for idx, (text, vec) in enumerate(zip(m["chunks"], vectors)):
                    cur = db.execute(
                        "INSERT INTO chunks (entry_id, chunk_index, text) VALUES (?,?,?)",
                        (m["entry_id"] or m["fingerprint"], idx, text))
                    db.execute("INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)",
                               (cur.lastrowid, sqlite_vec.serialize_float32(vec)))

            done_chunks += len(m["chunks"])
            if i % COMMIT_EVERY == 0 or i == len(todo):
                db.commit()
                ob.checkpoint("committed", emails_done=i, emails_total=len(todo),
                              chunks_done=done_chunks)
                elapsed = time.time() - started
                rate = done_chunks / elapsed if elapsed else 0
                left = (n_chunks - done_chunks) / rate if rate else 0
                print(f"  {i}/{len(todo)} emails | {done_chunks}/{n_chunks} chunks | "
                      f"{rate:.1f} chunks/s | ~{left / 60:.0f} min left", flush=True)

    total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print(f"\nDone in {(time.time() - started) / 60:.1f} min. "
          f"Database now holds {total} emails: {db_path}")
    ob.finish("ok", emails_processed=len(todo), chunks_embedded=done_chunks,
              database_emails=total)
    db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Finished emails are saved; run again to continue.")
        obs.finish_active("interrupted")
    except RuntimeError as err:
        print(f"\nERROR: {err}")
        obs.finish_active("failed", err)
        sys.exit(1)
    except SystemExit:
        obs.finish_active("stopped")
        raise
    except Exception as err:
        obs.finish_active("crashed", err)
        raise