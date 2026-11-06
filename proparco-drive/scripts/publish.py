#!/usr/bin/env python3
"""
publish.py - put the server vector database (server.db) on the shared server folder, safely.

    start.bat publish.py --from C:\\server-agent\\db\\server.db
    start.bat publish.py --from C:\\server-agent\\db\\server.db --to S:\\Proparco\\AgentServerVectorDB

Steps: check the local database -> copy it to the server under a temporary name -> check the
copy -> replace the published file in one step. Readers never see a half-copied database.
Run it when no build is writing to the local database (e.g. after the overnight run).
"""
import argparse
import json
import os
import platform
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import load_config  # noqa: E402

DEFAULT_TARGET = r"S:\Proparco\AgentServerVectorDB"


def inspect(path):
    """(ok, message, counts) for a database file, opened read-only."""
    try:
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        result = db.execute("PRAGMA quick_check").fetchone()[0]
        counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("messages", "chunks")
                  if db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (t,)).fetchone()}
        db.close()
    except sqlite3.Error as err:
        return False, f"cannot be read ({err})", {}
    return result == "ok", result, counts


def main():
    cfg = load_config()
    published = cfg.get("server_db_path")
    ap = argparse.ArgumentParser(description="Publish server.db to the shared server folder.")
    ap.add_argument("--from", dest="src", default=str(cfg["_data_dir"] / "db" / "server.db"),
                    help="the local server.db (default: the data folder's db\\server.db)")
    ap.add_argument("--to", dest="dst",
                    default=str(Path(published).parent) if published else DEFAULT_TARGET,
                    help=r"server folder (default: S:\Proparco\AgentServerVectorDB)")
    args = ap.parse_args()

    src, dst_dir = Path(args.src), Path(args.dst)
    if not src.exists():
        sys.exit(f"Not found: {src}")
    # Is a build still writing? Try to take the write lock for a moment.
    try:
        probe = sqlite3.connect(str(src), timeout=2)
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
        probe.close()
    except sqlite3.OperationalError:
        sys.exit(f"{src.name} is being written by a running build. Stop it first, then publish.")

    print(f"Checking {src} ...", flush=True)
    ok, msg, counts = inspect(src)
    if not ok:
        sys.exit(f"The local database is not healthy: {msg}. Not publishing.")
    size_mb = src.stat().st_size / 1024 / 1024
    print(f"   healthy: {counts.get('messages', 0):,} documents, {counts.get('chunks', 0):,} chunks, "
          f"{size_mb:,.0f} MB")

    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        sys.exit(f"Cannot create {dst_dir} ({err}). Check the network drive and your rights.")
    tmp, final = dst_dir / "server.db.tmp", dst_dir / "server.db"

    # 1. A complete, consistent snapshot next to the source (SQLite's backup also includes
    #    recent changes still held in the -wal side file, which a plain file copy would miss).
    snapshot = src.with_name("server.publish-snapshot.db")
    snapshot.unlink(missing_ok=True)
    s_db, t_db = sqlite3.connect(str(src)), sqlite3.connect(str(snapshot))
    s_db.backup(t_db)
    t_db.execute("PRAGMA journal_mode = DELETE")      # one self-contained file
    t_db.close()
    s_db.close()
    ok, msg, counts = inspect(snapshot)
    if not ok:
        snapshot.unlink(missing_ok=True)
        sys.exit(f"The snapshot is not healthy ({msg}). Not publishing.")
    size_mb = snapshot.stat().st_size / 1024 / 1024

    # 2. Copy that single file to the server under a temporary name.
    print(f"Copying to {tmp} ...", flush=True)
    t0 = time.time()
    shutil.copyfile(snapshot, tmp)
    snapshot.unlink(missing_ok=True)
    print(f"   copied in {time.time() - t0:,.0f} s")

    print("Checking the copy on the server ...", flush=True)
    ok, msg, copy_counts = inspect(tmp)
    if not ok or copy_counts != counts:
        tmp.unlink(missing_ok=True)
        sys.exit(f"The copy on the server is not identical ({msg}; {copy_counts} vs {counts}). "
                 "Nothing was replaced; try again.")

    try:
        os.replace(tmp, final)
    except OSError as err:
        sys.exit(f"Could not replace {final} ({err}). Someone may have it open; the checked copy "
                 f"is kept as {tmp.name}. Try again when nobody is searching.")

    info = {"published_at": datetime.now().isoformat(timespec="seconds"),
            "published_by": platform.node(), "source": str(src),
            "documents": counts.get("messages", 0), "chunks": counts.get("chunks", 0),
            "size_mb": round(size_mb, 1)}
    (dst_dir / "server.db.info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    print(f"\nPublished: {final}")
    print(f"   {info['documents']:,} documents, {info['chunks']:,} chunks, {info['size_mb']:,} MB")
    print('Search on any machine reads it when settings.json has "server_db_path" pointing to it.')


if __name__ == "__main__":
    main()