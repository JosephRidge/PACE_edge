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
    path = cfg["_data_dir"] / "db" / "proparco.db"
    if not path.exists():
        sys.exit(f"No database at {path}. Run embed.py first.")
    import sqlite_vec
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)

    with Engines(cfg, only=["embedding"]) as eng:
        t0 = time.time()
        qvec = eng.embed([cfg["embedding"].get("query_prefix", "") + args.query])[0]
        q = sqlite_vec.serialize_float32(qvec)
    print(f"\nQuery embedded in {time.time() - t0:.1f}s\n")

    if not args.summaries:
        rows = db.execute("""
            SELECT v.distance, c.text, m.subject, m.sender, m.received_iso, m.conversation_id
            FROM vec_chunks v
            JOIN chunks c   ON c.id = v.rowid
            JOIN messages m ON m.entry_id = c.entry_id
            WHERE v.embedding MATCH ? AND k = ?
            ORDER BY v.distance""", (q, args.k)).fetchall()
        print(f"=== Top {len(rows)} email chunks (similarity: 1.00 = identical meaning) ===")
        for r in rows:
            print(f"\n[{1 - r['distance']:.2f}] {(r['received_iso'] or '')[:10]} | "
                  f"{r['subject']} | thread {r['conversation_id'][:12]}")
            print("    " + r["text"][:300].replace("\n", " ") + ("..." if len(r["text"]) > 300 else ""))

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
            print(f"\n[{1 - r['distance']:.2f}] thread {r['conversation_id'][:12]}")
            print("    " + r["text"][:400].replace("\n", " | "))
    elif args.summaries:
        print("No thread summaries yet. Run generate.py first.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as err:
        print(f"\nERROR: {err}")
        sys.exit(1)