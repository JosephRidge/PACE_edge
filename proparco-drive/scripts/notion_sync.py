#!/usr/bin/env python3
"""
notion_sync.py - download a local directory of Notion projects, counterparties and contacts.

    start.bat notion_sync.py

Read-only. Keeps only what matching needs: names, emails, links between the three databases,
status/team/countries and page links. Saved as <data folder>\\notion\\directory.json and
used by notion_match.py and notion_push.py. Run it again to refresh (e.g. daily).
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import load_config  # noqa: E402
from notion_client import (Notion, NotionError, list_of, notion_settings,  # noqa: E402
                           relation_ids, text_of)


def main():
    cfg = load_config()
    ns = notion_settings(cfg)
    api = Notion(cfg)
    pf, cf, kf = ns["project_fields"], ns["counterparty_fields"], ns["contact_fields"]

    def fetch(label, ds):
        print(f"Reading {label} ...", flush=True)
        pages = api.query_all(ds, progress=lambda n: print(f"   {n:,}", end="\r", flush=True))
        print(f"   {len(pages):,} {label}        ")
        return pages

    projects = [{
        "id": p["id"], "url": p.get("url", ""),
        "name": text_of(p, pf["title"]),
        "project_id": text_of(p, pf["project_id"]),
        "status": text_of(p, pf["status"]),
        "team": list_of(p, pf["team"]),
        "countries": list_of(p, pf["countries"]),
        "counterparties": sorted({cid for f in pf["counterparty_links"] for cid in relation_ids(p, f)}),
        # current text of the three fields, to show next to the assistant's proposals
        "latest": text_of(p, pf["latest"])[:4000],
        "history": text_of(p, pf["history"])[:4000],
        "next": text_of(p, pf["next"])[:4000],
        "edited": p.get("last_edited_time", ""),
    } for p in fetch("projects", ns["projects"]) if not p.get("in_trash") and not p.get("archived")]

    counterparties = [{
        "id": p["id"], "url": p.get("url", ""),
        "name": text_of(p, cf["title"]),
        "projects": relation_ids(p, cf["projects"]),
        "last_interaction": text_of(p, cf["last_interaction"]),
        "geography": list_of(p, cf["geography"]),
        "type": list_of(p, cf["type"]),
    } for p in fetch("counterparties", ns["counterparties"])
        if not p.get("in_trash") and not p.get("archived")]

    contacts = [{
        "id": p["id"],
        "name": " ".join(x for x in (text_of(p, kf["first"]), text_of(p, kf["last"])) if x),
        "email": text_of(p, kf["email"]).lower(),
        "organisations": relation_ids(p, kf["organisation"]),
        "job": text_of(p, kf["job"]),
    } for p in fetch("contacts", ns["contacts"]) if not p.get("in_trash") and not p.get("archived")]

    out_dir = cfg["_data_dir"] / "notion"
    out_dir.mkdir(parents=True, exist_ok=True)
    directory = {"synced_at": datetime.now().isoformat(timespec="seconds"),
                 "projects": projects, "counterparties": counterparties, "contacts": contacts}
    tmp = out_dir / "directory.json.tmp"
    tmp.write_text(json.dumps(directory, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_dir / "directory.json")

    with_email = sum(1 for c in contacts if c["email"])
    print(f"\nSaved {out_dir / 'directory.json'}")
    print(f"   {len(projects):,} projects, {len(counterparties):,} counterparties, "
          f"{len(contacts):,} contacts ({with_email:,} with an email address); "
          f"{api.calls} Notion requests")
    print("Read-only: nothing was changed in Notion.")


if __name__ == "__main__":
    try:
        main()
    except NotionError as err:
        sys.exit(f"ERROR: {err}")