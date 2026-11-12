#!/usr/bin/env python3
"""
preflight.py - is everything ready to run? A read-only check of the whole architecture.

    python3 -B scripts/preflight.py                 (quick: files, databases, page build)
    python3 -B scripts/preflight.py --full          (also loads the models and runs one real search)

Uses PROPARCO_DATA_DIR (the mailbox to demo, e.g. ~/proparco-work/mailboxes/Diana); its parent
folder is scanned for the other mailboxes. Prints GO / NO-GO per item and a verdict at the end.
Read-only: databases are opened read-only; the only file written is the deal page (deals.html),
rebuilt exactly as deals_view.py does.
"""
import argparse
import json
import re
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from engine import load_config  # noqa: E402

RESULTS = []


def report(stage, item, ok, detail="", warn=False):
    mark = "WARN " if warn else ("GO   " if ok else "NO-GO")
    RESULTS.append((stage, item, ok, warn))
    print(f"  {mark} {item:34} {detail}")


def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def db_facts(path, vectors=True):
    """(integrity, documents, chunks, vectors) of an index, read-only."""
    db = ro(path)
    ok = db.execute("PRAGMA quick_check").fetchone()[0]
    n = db.execute("SELECT count(*) FROM messages").fetchone()[0]
    c = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    v = None
    if vectors:
        try:
            v = db.execute("SELECT count(*) FROM vec_chunks_rowids").fetchone()[0]
        except sqlite3.Error:
            v = None
    db.close()
    return ok, n, c, v


def main():
    ap = argparse.ArgumentParser(description="Read-only readiness check of the whole pipeline.")
    ap.add_argument("--full", action="store_true", help="also load the models and run one real search")
    args = ap.parse_args()
    cfg = load_config()
    data = Path(cfg["_data_dir"])
    prog = HERE.parent
    t0 = time.time()
    print(f"Pre-flight {datetime.now():%d/%m/%Y %H:%M} | mailbox: {data}\n")

    # ------------------------------------------------------------ 1. data sources
    print("1. Data sources")
    boxes = sorted(p for p in data.parent.iterdir() if (p / "acquisitions").is_dir()) if data.parent.is_dir() else []
    report(1, "mailbox to demo", (data / "acquisitions").is_dir(), str(data))
    report(1, "other mailboxes found", True, ", ".join(b.name for b in boxes if b != data) or "none")
    from sources import server_databases
    sdbs = server_databases(cfg)
    report(1, "sector databases", bool(sdbs), ", ".join(label for label, _ in sdbs) or "none found: check server_db_dir")

    # ------------------------------------------------------------ 2. extract
    print("\n2. Extract (email exports)")
    for b in boxes:
        logs = sorted((b / "logs").glob("mail-export-*.log")) if (b / "logs").is_dir() else []
        if not logs:
            report(2, f"{b.name}: export log", False, "no log found", warn=True)
            continue
        txt = logs[-1].read_text(encoding="utf-8", errors="replace")
        n = re.findall(r"([\d,]+) messages in [\d,]+ threads", txt)
        verdict = re.findall(r"(ALL ITEMS ACCOUNTED FOR|ACCOUNTED FOR WITH EXCEPTIONS|STOPPED)", txt)
        v = verdict[-1] if verdict else "no verdict"
        report(2, f"{b.name}: export", v.startswith("ALL") or v.startswith("ACCOUNTED"),
               f"{n[-1] if n else '?'} emails | {v}", warn=v.startswith("ACCOUNTED"))

    # ------------------------------------------------------------ 3. clean and index
    print("\n3. Clean and index")
    for b in boxes:
        p = b / "db" / "proparco.db"
        if not p.exists():
            report(3, f"{b.name}: email index", False, "not indexed yet", warn=b != data)
            continue
        try:
            ok, n, c, v = db_facts(p)
            good = ok == "ok" and (v is None or v == c)
            report(3, f"{b.name}: email index", good, f"{n:,} emails | {c:,} pieces | vectors {v if v is not None else '?'} | {ok}")
        except Exception as err:
            report(3, f"{b.name}: email index", False, f"BROKEN: {err}")
    for label, p in sdbs:
        try:
            ok, n, c, v = db_facts(p)
            good = ok == "ok" and (v is None or v == c)
            report(3, f"sector {label}", good, f"{n:,} documents | {c:,} pieces | vectors {v if v is not None else '?'} | {ok}")
        except Exception as err:
            report(3, f"sector {label}", False, f"BROKEN: {err}")

    # ------------------------------------------------------------ 4. generate
    print("\n4. Generate (models and summariser)")
    for role in ("embedding", "generation"):
        # the same settings the engine uses: a .gguf model, or a self-contained .llamafile (binary)
        m = cfg.get(role, {}).get("model") or cfg.get(role, {}).get("binary")
        mp = (prog / m) if m and not Path(m).is_absolute() else (Path(m) if m else None)
        if mp is None or not mp.exists():
            files = sorted(list((prog / "models").glob("*.gguf")) + list((prog / "models").glob("*.llamafile"))) \
                if (prog / "models").is_dir() else []
            mp = next((f for f in files if ("bge" in f.name.lower()) == (role == "embedding")), mp)
        report(4, f"{role} model", bool(mp and mp.exists()),
               f"{mp.name} ({mp.stat().st_size / 1e9:.1f} GB)" if mp and mp.exists() else "not found")
    try:
        import sqlite_vec  # noqa: F401
        report(4, "vector extension (sqlite-vec)", True, "available")
    except Exception as err:
        report(4, "vector extension (sqlite-vec)", False, f"{err}: activate the environment")
    bad = [f.name for f in sorted(HERE.glob("*.py"))
           if subprocess.run([sys.executable, "-B", "-m", "py_compile", str(f)], capture_output=True).returncode]
    report(4, "all scripts compile", not bad, ", ".join(bad) or f"{len(list(HERE.glob('*.py')))} scripts")
    ndir = data / "notion"
    try:
        d = json.loads((ndir / "directory.json").read_text(encoding="utf-8"))
        age = (time.time() - (ndir / "directory.json").stat().st_mtime) / 86400
        report(4, "Notion directory (local copy)", True, f"{len(d['projects'])} deals | {age:.0f} day(s) old")
    except Exception as err:
        report(4, "Notion directory (local copy)", False, str(err)[:80])
    try:
        m = json.loads((ndir / "matches.json").read_text(encoding="utf-8"))
        report(4, "email-to-deal matches", bool(m), f"{sum(1 for x in m if x.get('candidates'))} of {len(m)} threads matched")
    except Exception as err:
        report(4, "email-to-deal matches", False, f"{err}: run notion_match.py")

    # ------------------------------------------------------------ 5. verify and review
    print("\n5. Verify and review")
    try:
        db = ro(data / "db" / "proparco.db")
        rows = db.execute("SELECT review_status, count(*) FROM deal_proposals GROUP BY review_status").fetchall()
        total = sum(n for _, n in rows)
        report(5, "deal summaries", total > 0, ", ".join(f"{n} {s}" for s, n in rows) or "none yet")
        pushed = db.execute("SELECT count(*) FROM deal_proposals WHERE notion_status != 'not_pushed'").fetchone()[0]
        report(5, "nothing pushed to Notion", pushed == 0, f"{pushed} pushed")
        db.close()
    except Exception as err:
        report(5, "deal summaries", False, str(err)[:80])

    # ------------------------------------------------------------ 6. use
    print("\n6. Use (deal page, search, chat)")
    try:
        import deals_view
        out = deals_view.build(cfg, quiet=True)
        html = Path(out).read_text(encoding="utf-8")
        report(6, "deal page builds", "const DATA = " in html, f"{out} ({len(html) / 1e6:.1f} MB)")
    except Exception as err:
        report(6, "deal page builds", False, str(err)[:80])
    for port, name in ((8765, "chat port 8765 free"), (8766, "duplicate-review port 8766 free")):
        s = socket.socket()
        busy = s.connect_ex(("127.0.0.1", port)) == 0
        s.close()
        report(6, name, not busy, "in use: stop the other program first" if busy else "free", warn=busy)
    if args.full:
        try:
            r = subprocess.run([sys.executable, "-B", str(HERE / "search.py"), "bond issuance timetable", "--k", "3"],
                               capture_output=True, text=True, timeout=600)
            hits = r.stdout.count("\n")
            report(6, "real search (models loaded)", r.returncode == 0 and hits > 2,
                   f"{hits} output lines" if r.returncode == 0 else (r.stderr or r.stdout)[-120:])
        except Exception as err:
            report(6, "real search (models loaded)", False, str(err)[:80])
    else:
        report(6, "real search", True, "skipped (add --full to load the models and search)", warn=True)

    # ------------------------------------------------------------ 7. Notion push
    print("\n7. Notion push (next phase)")
    # A write to Notion is a PATCH or DELETE, or a POST that creates pages or blocks, whether through
    # the shared client (.call) or a direct request (method="PATCH"). Reading (POST .../query,
    # POST /search, GET) is allowed.
    write = re.compile(r"""\.call\(\s*["'](PATCH|DELETE)["']|\.call\(\s*["']POST["']\s*,\s*f?["']/(pages|blocks)"""
                       r"""|method\s*=\s*["'](PATCH|DELETE)["']""")
    pushers = [f.name for f in HERE.glob("*.py") if f.name != "preflight.py" and write.search(f.read_text(errors="ignore"))]
    report(7, "no script writes to Notion", not pushers, ", ".join(pushers) or "read-only by design: only searches Notion")

    # ------------------------------------------------------------ verdict
    nogo = [(s, i) for s, i, ok, w in RESULTS if not ok and not w]
    warns = [(s, i) for s, i, ok, w in RESULTS if w]
    print(f"\n{'=' * 70}\nVERDICT: {'GO' if not nogo else 'NO-GO'}  ({len(RESULTS)} checks, {len(nogo)} NO-GO, "
          f"{len(warns)} warning(s), {time.time() - t0:.0f} s)")
    for s, i in nogo:
        print(f"  fix first: stage {s}, {i}")
    sys.exit(1 if nogo else 0)


if __name__ == "__main__":
    main()