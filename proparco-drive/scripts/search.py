#!/usr/bin/env python3
"""
search.py - test the sqlite-vec search: find email chunks and thread summaries by meaning.

Run from the proparco-drive folder (or from scripts/):
    python3 scripts/search.py "land rights blocking the ESG audit"
    python3 scripts/search.py "prêt solaire Kenya" --k 8
    python3 scripts/search.py "signed NDA" --summaries     # thread summaries only

Works across languages: a French query can find English or German emails.
Output stays in this terminal; nothing is sent anywhere.
"""
import argparse
import sqlite3
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import Engines, load_config  # noqa: E402


HEADER = ("Document:", "Title:", "Author:", "Last edited by:", "Created:")


def snippet(text):
    """Chunk text for display: without the document header lines, on one line."""
    lines = text.split("\n")
    while lines and (lines[0].startswith(HEADER) or not lines[0].strip()):
        lines.pop(0)
    return " ".join(" ".join(lines).split()).replace("\\", "/")


def dedupe(rows, k):
    """One entry per document and passage: the PDF and Word versions of the same file
    (same folder and name) are shown once, noting the other formats."""
    out, seen = [], {}
    for r in rows:
        name = r["subject"] or ""
        stem, _, ext = name.rpartition(".")
        key_doc = (r["conversation_id"], (stem or name).lower())
        plain = re.sub(r"\[(Page|Slide|Sheet) [^\]]*\]", " ", snippet(r["text"]))
        key_text = " ".join(plain.split())[:120].lower()
        prev = seen.get((key_doc, key_text))
        if prev is not None:
            if ext and ext.lower() not in prev["also"] and not name.lower().endswith(prev["ext"]):
                prev["also"].append(ext.lower())
            continue
        item = dict(r)
        item["also"], item["ext"] = [], "." + ext.lower() if ext else ""
        seen[(key_doc, key_text)] = item
        out.append(item)
        if len(out) == k:
            break
    return out


def main():
    ap = argparse.ArgumentParser(description="Semantic search over the local database.")
    ap.add_argument("query")
    ap.add_argument("--k", type=int, default=5, help="number of results (default 5)")
    ap.add_argument("--summaries", action="store_true", help="search thread summaries only")
    args = ap.parse_args()

    cfg = load_config()
    import sqlite_vec
    # Emails: proparco.db on the drive. Documents: the published server copy if reachable,
    # otherwise the local build (server.db on the drive).
    from sources import server_databases
    sources = [("emails", cfg["_data_dir"] / "db" / "proparco.db")]
    sources += [(f"documents ({label})", path) for label, path in server_databases(cfg)]
    sources = [(k, p) for k, p in sources if p.exists()]
    if not sources:
        sys.exit("No database yet. Run embed.py (emails) or embed.py --documents (server files).")

    def connect(path, read_only):
        uri = f"file:{Path(path).as_posix()}?mode=ro" if read_only else str(path)
        con = sqlite3.connect(uri, uri=read_only)
        con.row_factory = sqlite3.Row
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        return con

    with Engines(cfg, only=["embedding"]) as eng:
        t0 = time.time()
        qvec = eng.embed([cfg["embedding"].get("query_prefix", "") + args.query])[0]
        q = sqlite_vec.serialize_float32(qvec)
    print(f"\nQuery embedded in {time.time() - t0:.1f}s\n")

    for kind, path in sources:
        is_docs = kind.startswith("documents")
        db = connect(path, read_only=is_docs)
        if not args.summaries:
            rows = db.execute("""
                SELECT v.distance, c.text, m.subject, m.sender, m.received_iso, m.conversation_id
                FROM vec_chunks v
                JOIN chunks c   ON c.id = v.rowid
                JOIN messages m ON m.entry_id = c.entry_id
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance""", (q, args.k * 4)).fetchall()
            rows = dedupe(rows, args.k) if is_docs else [dict(r) for r in rows[:args.k]]   # plain dicts, like documents
            label = kind[:-1] if kind == "emails" else kind
            print(f"\n=== Top {len(rows)} {label} chunks from {path.name} "
                  f"(similarity: 1.00 = identical meaning) ===")
            for r in rows:
                where = (r["conversation_id"].replace("folder:", "folder ")[:40] if is_docs
                         else f"thread {r['conversation_id'][:12]}")
                print(f"\n[{(1 - r['distance']) if r['distance'] is not None else 0:.2f}] {(r['received_iso'] or '')[:10]} | "
                      f"{r['subject']} | {where}")
                text = snippet(r["text"])
                also = f"  (same document also as {', '.join(r['also'])})" if r.get("also") else ""
                print("    " + text[:300] + ("..." if len(text) > 300 else "") + also)
        if kind == "emails":
            has_summaries = db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'thread_summaries'").fetchone()
            if has_summaries and db.execute("SELECT COUNT(*) FROM thread_summaries").fetchone()[0]:
                rows = db.execute("""
                    SELECT v.distance, s.text, s.conversation_id
                    FROM vec_thread_summaries v
                    JOIN thread_summaries s ON s.id = v.rowid
                    WHERE v.embedding MATCH ? AND k = ?
                    ORDER BY v.distance""", (q, args.k)).fetchall()
                print(f"\n=== Top {len(rows)} thread summaries ===")
                for r in rows:
                    print(f"\n[{(1 - r['distance']) if r['distance'] is not None else 0:.2f}] thread {r['conversation_id'][:12]}")
                    print("    " + r["text"][:400].replace("\n", " | "))
            elif args.summaries:
                print("No thread summaries yet. Run generate.py first.")
        db.close()

if __name__ == "__main__":
    try:
        main()
    except RuntimeError as err:
        print(f"\nERROR: {err}")
        sys.exit(1)