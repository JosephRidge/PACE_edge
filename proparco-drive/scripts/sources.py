"""
sources.py - where the server-document databases are, for every script that reads them.

One place decides which sector databases exist on this laptop, so the chat, the deal
summaries, the deal pages and search always read the SAME set:
  - "server_db_path" in settings (one published database), if it exists
  - every *.db in "server_db_dir" (a folder of sector databases: CROSS.db, ENI.db, ...)
  - the data folder's own db/server.db
Each comes with a label (the sector name) so evidence can say where it came from.
"""
from pathlib import Path


def server_databases(cfg):
    """[(label, path), ...] of every server database on this laptop, without duplicates."""
    found, seen = [], set()
    candidates = []
    if cfg.get("server_db_path"):
        candidates.append(Path(cfg["server_db_path"]))
    if cfg.get("server_db_dir") and Path(cfg["server_db_dir"]).is_dir():
        candidates += sorted(Path(cfg["server_db_dir"]).glob("*.db"))
    candidates.append(Path(cfg["_data_dir"]) / "db" / "server.db")
    for p in candidates:
        try:
            if not p.exists() or p.resolve() in seen:
                continue
        except OSError:
            continue
        seen.add(p.resolve())
        label = p.parent.parent.name if p.name == "server.db" and p.parent.name == "db" else p.stem
        if label.lower() in ("proparco-data", "data", "server-agent", ""):
            label = "server"
        found.append((label, p))
    return found
