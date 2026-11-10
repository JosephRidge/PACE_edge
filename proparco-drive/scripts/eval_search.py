#!/usr/bin/env python3
"""
eval_search.py - measure how well and how fast search finds the right documents.

    python3 scripts/eval_search.py --db /path/to/CROSS/db/server.db \
        --questions scripts/eval/questions_cross.json

Uses the same search path as the chat (engine, query prefix, vector query, one result per
document). For each question: time to embed the question, time to search, the top
documents found, and the rank of the expected document. Questions with no expected
document are controls: their best similarity should be clearly lower.
Read-only on the database. Writes eval-<db>-<date>.md next to the current folder.
"""
import argparse
import json
import re
import sqlite3
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def top_documents(con, qbytes, k):
    """Nearest chunks, then one entry per document (best chunk), like the chat."""
    rows = con.execute("""
        SELECT v.distance, c.text, m.subject, m.conversation_id
        FROM vec_chunks v
        JOIN chunks c   ON c.id = v.rowid
        JOIN messages m ON m.entry_id = c.entry_id
        WHERE v.embedding MATCH ? AND k = ?
        ORDER BY v.distance""", (qbytes, k * 6)).fetchall()
    docs, seen = [], set()
    for dist, text, subject, conv in rows:
        key = ((conv or "").lower(), (subject or "").rsplit(".", 1)[0].lower())
        if key in seen:
            continue
        seen.add(key)
        docs.append({"subject": subject or "", "folder": (conv or "").replace("folder:", ""),
                     "similarity": 1 - dist if dist is not None else 0.0,
                     "snippet": " ".join((text or "").split())[:160]})
        if len(docs) == k:
            break
    return docs


def rank_of(docs, expect):
    want = [e.lower() for e in expect]
    for i, d in enumerate(docs, 1):
        hay = f"{d['subject']} {d['folder']}".lower()
        if any(w in hay for w in want):
            return i
    return None


def score(results):
    """hit@1/5/10 and MRR over questions that have an expected document."""
    rated = [r for r in results if r["expect"]]
    n = max(1, len(rated))
    ranks = [r["rank"] for r in rated]
    return {"n": len(rated),
            "hit1": sum(1 for x in ranks if x == 1) / n,
            "hit5": sum(1 for x in ranks if x and x <= 5) / n,
            "hit10": sum(1 for x in ranks if x and x <= 10) / n,
            "mrr": sum(1 / x for x in ranks if x) / n}


def render(db, results, s, k):
    emb = [r["t_embed"] for r in results]
    srch = [r["t_search"] for r in results]
    found = [r["best"] for r in results if r["expect"] and r["rank"]]
    ctrl = [r["best"] for r in results if not r["expect"]]
    L = [f"# Search evaluation: {db}", f"{datetime.now():%d %B %Y, %H:%M}", "",
         f"**{s['n']} questions with a known answer:** found first {100*s['hit1']:.0f}% | "
         f"in top 5 {100*s['hit5']:.0f}% | in top {k} {100*s['hit10']:.0f}% | MRR {s['mrr']:.2f}", "",
         f"**Speed:** embedding the question median {statistics.median(emb):.2f}s (max {max(emb):.2f}s); "
         f"search median {statistics.median(srch):.2f}s (max {max(srch):.2f}s)", ""]
    if found and ctrl:
        L += [f"**Similarity:** correct answers {min(found):.2f}-{max(found):.2f} (median {statistics.median(found):.2f}); "
              f"no-answer controls {min(ctrl):.2f}-{max(ctrl):.2f}. "
              + ("Controls are clearly lower: a threshold can flag 'nothing relevant found'."
                 if max(ctrl) < statistics.median(found) else
                 "Controls overlap with real answers: similarity alone cannot tell 'nothing found'."), ""]
    L += ["| ID | Lang | Question | Rank | Best sim | Top 3 documents |", "|---|---|---|---|---|---|"]
    for r in results:
        rk = ("control" if not r["expect"] else (f"**{r['rank']}**" if r["rank"] else "not found"))
        top3 = "; ".join(d["subject"][:45] for d in r["docs"][:3])
        L.append(f"| {r['id']} | {r['lang']} | {r['q'][:60]} | {rk} | {r['best']:.2f} | {top3} |")
    misses = [r for r in results if r["expect"] and not r["rank"]]
    if misses:
        L += ["", "## Not found in the top results (what came up instead)"]
        for r in misses:
            L += ["", f"**{r['id']}** {r['q']}  (expected: {', '.join(r['expect'])})"]
            L += [f"- {d['similarity']:.2f} {d['subject']} | {d['folder'][-60:]}" for d in r["docs"][:5]]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Measure search quality and speed on a sector database.")
    ap.add_argument("--db", required=True, help="the sector's server.db")
    ap.add_argument("--questions", required=True, help="questions JSON (see scripts/eval/questions_cross.json)")
    ap.add_argument("--k", type=int, default=10, help="documents considered per question (default 10)")
    ap.add_argument("--out", default=".", help="folder for the report")
    args = ap.parse_args()

    import sqlite_vec
    from engine import Engines, load_config
    cfg = load_config()
    qs = json.loads(Path(args.questions).read_text(encoding="utf-8"))["questions"]
    db = Path(args.db)
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.enable_load_extension(True); sqlite_vec.load(con); con.enable_load_extension(False)
    n_chunks = con.execute("select count(*) from chunks").fetchone()[0]
    print(f"Database: {db} ({n_chunks:,} chunks). {len(qs)} questions.\n")

    results = []
    with Engines(cfg, only=["embedding"]) as eng:
        prefix = cfg["embedding"].get("query_prefix", "")
        for q in qs:
            t0 = time.time()
            vec = eng.embed([prefix + q["q"]])[0]
            t1 = time.time()
            docs = top_documents(con, sqlite_vec.serialize_float32(vec), args.k)
            t2 = time.time()
            r = {**q, "docs": docs, "t_embed": t1 - t0, "t_search": t2 - t1,
                 "rank": rank_of(docs, q["expect"]) if q["expect"] else None,
                 "best": docs[0]["similarity"] if docs else 0.0}
            results.append(r)
            mark = "control" if not q["expect"] else (f"rank {r['rank']}" if r["rank"] else "NOT FOUND")
            print(f"{q['id']}  {mark:<10} {r['best']:.2f}  {r['t_search']:.2f}s  {q['q'][:60]}")

    s = score(results)
    text = render(db.parent.parent.name or db.name, results, s, args.k)
    out = Path(args.out) / f"eval-{db.parent.parent.name or 'db'}-{datetime.now():%Y%m%d-%H%M}.md"
    out.write_text(text, encoding="utf-8")
    print("\n" + text.split("| ID |")[0])
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()