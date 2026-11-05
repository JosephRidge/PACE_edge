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
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import Engines, load_config  # noqa: E402


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
    sources = [("emails", cfg["_data_dir"] / "db" / "proparco.db")]
    published = cfg.get("server_db_path")
    local_docs = cfg["_data_dir"] / "db" / "server.db"
    if published and Path(published).exists():
        sources.append(("documents", Path(published)))
    elif local_docs.exists():
        sources.append(("documents", local_docs))
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
        db = connect(path, read_only=(kind == "documents"))
        if not args.summaries:
            rows = db.execute("""
                SELECT v.distance, c.text, m.subject, m.sender, m.received_iso, m.conversation_id
                FROM vec_chunks v
                JOIN chunks c   ON c.id = v.rowid
                JOIN messages m ON m.entry_id = c.entry_id
                WHERE v.embedding MATCH ? AND k = ?
                ORDER BY v.distance""", (q, args.k)).fetchall()
            label = "document" if kind == "documents" else "email"
            print(f"\n=== Top {len(rows)} {label} chunks from {path.name} "
                  f"(similarity: 1.00 = identical meaning) ===")
            for r in rows:
                where = (r["conversation_id"].replace("folder:", "folder ")[:40] if kind == "documents"
                         else f"thread {r['conversation_id'][:12]}")
                print(f"\n[{(1 - r['distance']) if r['distance'] is not None else 0:.2f}] {(r['received_iso'] or '')[:10]} | "
                      f"{r['subject']} | {where}")
                print("    " + r["text"][:300].replace("\n", " ").replace("\\", "/")
                      + ("..." if len(r["text"]) > 300 else ""))
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