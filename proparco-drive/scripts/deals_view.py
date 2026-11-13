#!/usr/bin/env python3
"""
deals_view.py - a deal-centred page: Notion deals, with their emails and server documents.

    start.bat notion_sync.py      (Notion directory, incl. current field text)
    start.bat notion_match.py     (which email threads belong to which deals)
    start.bat deals_view.py       -> <data folder>\\deals.html, opened in the browser

For each deal: Notion facts and a link to the page; the three fields as they are in Notion
next to the assistant's latest proposal from the emails; the matched email threads (with the
reasons for the match); related server documents; contacts. Read-only: nothing is sent anywhere.
"""
import argparse
import glob
import html
import json
import re
import sqlite3
import sys
import webbrowser
from collections import defaultdict
import os
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import load_config  # noqa: E402
import viewer  # noqa: E402   (same email/attachment data as the email view)

INACTIVE = ("closed", "repaid", "exited", "written-off", "dropped", "abandoned")
DOCS_PER_DEAL = 8
STALE_AFTER = 15 * 60     # seconds without news from the summariser = interrupted


def esc(x):
    return html.escape(str(x or ""))


def para(x):
    x = (x or "").strip()
    return esc(x).replace("\n", "<br>") if x else '<span class="none">(empty)</span>'


def read_db(path):
    try:
        db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    return db


MAX_EMAILS_PER_THREAD = 5   # newest emails shown per thread (the agent summary covers them all)
MAX_OPEN_THREADS = 5        # newest threads shown open per deal; older ones folded
EMAIL_TEXT_CHARS = 3000     # email text kept on the page (the full text stays in the database)


def email_threads(db_path):
    """conversation_id -> thread info incl. the latest generated fields."""
    if not Path(db_path).exists():
        return {}
    db = read_db(db_path)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    results = {}
    if "email_results" in tables:
        cols = {r[1] for r in db.execute("PRAGMA table_info(email_results)")}
        want = [c for c in ("latest_deal_summary", "project_history", "next_steps", "review_status")
                if c in cols]
        if want:
            for r in db.execute(f"SELECT entry_id, {', '.join(want)} FROM email_results"):
                results[r["entry_id"]] = dict(r)
    threads = {}
    for m in db.execute("""SELECT entry_id, conversation_id, subject, sender, recipients,
                                  received_iso, body_clean FROM messages ORDER BY received_iso"""):
        conv = m["conversation_id"] or ""
        if conv.startswith("folder:"):
            continue
        t = threads.setdefault(conv, {"subject": m["subject"] or "", "count": 0, "last": "",
                                      "senders": [], "result": None, "review": 0, "emails": []})
        try:
            to = json.loads(m["recipients"] or "[]")
        except ValueError:
            to = []
        t["emails"].append({"date": (m["received_iso"] or "")[:16].replace("T", " "),
                            "from": m["sender"] or "", "to": [str(x) for x in to][:6],
                            "subject": m["subject"] or "", "body": (m["body_clean"] or "")[:6000],
                            "flags": [], "review": None, "attachments": []})
        t["count"] += 1
        t.setdefault("generated", 0)
        t["last"] = max(t["last"], m["received_iso"] or "")
        if m["sender"] and m["sender"] not in t["senders"]:
            t["senders"].append(m["sender"])
        res = results.get(m["entry_id"])
        if res:
            t["generated"] += 1
            if res.get("review_status") == "needs_review":
                t["review"] += 1
            if any(res.get(k) for k in ("latest_deal_summary", "project_history", "next_steps")):
                t["result"] = res                       # the latest email with fields wins
    # The thread's current fields come from its final state (thread_state), which every
    # version of generate.py keeps, including the one that stores only per-email changes.
    if "thread_state" in tables:
        for r in db.execute("SELECT conversation_id, state_json FROM thread_state"):
            t = threads.get(r["conversation_id"])
            if t is None or not t["generated"]:
                continue
            try:
                fields = fields_from_state(json.loads(r["state_json"]))
            except (ValueError, TypeError, KeyError):
                continue
            t["result"] = {**(t["result"] or {}), **fields}
    db.close()
    return threads


def fields_from_state(state):
    """The three fields, rendered exactly as generate.py renders them."""
    import generate as G
    return {"latest_deal_summary": G.render_summary(state.get("summary")),
            "project_history": "\n".join(state.get("history", [])),
            "next_steps": G.render_steps(state.get("steps", []))}


def page_emails(emails):
    """The newest MAX_EMAILS_PER_THREAD emails of a thread, newest first, text capped."""
    newest = sorted(emails, key=lambda e: e.get("date") or "", reverse=True)[:MAX_EMAILS_PER_THREAD]
    return {"olderEmails": max(0, len(emails) - len(newest)),
            "emails": [{"date": e["date"], "from": e["from"], "to": e["to"][:6], "subject": e["subject"],
                        "body": (e["body"] or "")[:EMAIL_TEXT_CHARS],
                        "bodyCut": len(e["body"] or "") > EMAIL_TEXT_CHARS,
                        "flags": e["flags"], "review": e["review"], "attachments": e["attachments"]}
                       for e in newest]}


def server_db(cfg):
    published = cfg.get("server_db_path")
    if published and Path(published).exists():
        return Path(published)
    local = cfg["_data_dir"] / "db" / "server.db"
    return local if local.exists() else None


def mailbox_label(cfg):
    """Whose mailbox the page shows: the data folder's name and the mailbox's own address, taken from
    the export (each email's folder path starts with it, e.g. omondid@afd.fr\\Inbox). Read-only."""
    import collections
    name = cfg["_data_dir"].name
    stores = collections.Counter()
    for f in sorted(glob.glob(str(cfg["_data_dir"] / "acquisitions" / "*" / "acquisition.json")))[:3]:
        try:
            for m in json.loads(Path(f).read_text(encoding="utf-8-sig")).get("messages") or []:
                top = re.split(r"[\\/]", m.get("folderPath") or "", maxsplit=1)[0]
                if "@" in top:
                    stores[top] += 1
        except Exception:
            continue
    return {"name": name, "email": stores.most_common(1)[0][0] if stores else ""}


def doc_link(cfg, body):
    """(link, path) of the original file on the server, e.g. S:\\Proparco\\01. FII\\x.pdf.
    Built as a Windows path on any system, so links also appear when the page is built on Linux."""
    first = (body or "").split("\n", 1)[0]
    if not first.startswith("Document: "):
        return None, None
    parts = [x for x in re.split(r"[\\/]", first[len("Document: "):]) if x]
    base_text = cfg.get("sdrive_link_base", "S:\\")
    base = PureWindowsPath(base_text) if re.match(r"^[A-Za-z]:|^\\\\", base_text) else Path(base_text)
    if not parts or not base.is_absolute():
        return None, None
    full = base.joinpath(*parts)
    try:
        return full.as_uri(), str(full)
    except ValueError:
        return None, str(full)


NEWSLETTER = re.compile(r"(actualit|br[eè]ves|newsletter|revue de presse|press review|weekly|"
                        r"reporting mensuel|monthly report)", re.I)


def related_documents(cfg, db, names):
    """Server documents about the deal: named after it first (file name or folder), then
    documents that only mention it; newsletters and press reviews last; duplicates grouped."""
    names = [n for n in names if n and len(n) >= 4]
    if db is None or not names:
        return []
    where = " OR ".join(["subject LIKE ? OR body_clean LIKE ?"] * len(names))
    params = [v for n in names for v in (f"%{n}%", f"%{n}%")]
    rows = db.execute(f"""SELECT subject, received_iso, body_clean FROM messages WHERE {where}
                          ORDER BY received_iso DESC LIMIT 60""", params).fetchall()
    lowered = [n.lower() for n in names]
    found, seen = [], {}
    for r in rows:
        body = r["body_clean"] or ""
        first = body.split("\n", 1)[0].lower()
        name = r["subject"] or ""
        named = any(n in name.lower() or n in first for n in lowered)    # file or folder name
        news = bool(NEWSLETTER.search(name))
        key = re.sub(r"(-output)?\.[a-z0-9]+$", "", name.lower())          # same doc, other format
        if key in seen:
            if name.rsplit(".", 1)[-1].lower() not in seen[key]["also"]:
                seen[key]["also"].append(name.rsplit(".", 1)[-1].lower())
            continue
        text = body.split("\n\n", 1)[1] if "\n\n" in body else body
        link, path = doc_link(cfg, body)
        item = {"name": name, "date": (r["received_iso"] or "")[:10], "also": [],
                "excerpt": " ".join(text.split())[:300], "link": link, "path": path,
                "kind": "about this deal" if named else ("newsletter mention" if news else "mentions it"),
                "rank": 0 if named else (2 if news else 1)}
        seen[key] = item
        found.append(item)
    found.sort(key=lambda x: (x["rank"], -int(x["date"].replace("-", "") or 0)))
    return found[:DOCS_PER_DEAL]


def all_related_documents(cfg, sdbs, names):
    """related_documents() over every sector database, merged: the deal's own documents first,
    newest first, each tagged with its sector."""
    found = []
    for label, con in sdbs:
        try:
            for item in related_documents(cfg, con, names):
                item["sector"] = label
                found.append(item)
        except sqlite3.Error:
            continue
    found.sort(key=lambda x: (x["rank"], -int(x["date"].replace("-", "") or 0)))
    return found[:DOCS_PER_DEAL]


def core_name(deal_name):
    """'Family Bank - Equity 2026' -> 'Family Bank' (the part naming the company)."""
    return re.split(r"\s+[-–]\s+", deal_name or "", maxsplit=1)[0].strip()


def _json_list(text):
    try:
        v = json.loads(text or "[]")
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def deal_proposal(r):
    """A deal-level proposal (Notion + emails + documents) in the page's format."""
    try:
        src = json.loads(r.get("sources_json") or "{}")
    except ValueError:
        src = {}
    latest, base = r.get("proposed_latest") or "", (r.get("notion_latest") or "").strip()
    added = latest[len(base):].strip() if base and latest.startswith(base) else ("" if latest == base else latest)
    return {"kind": "deal", "latest": latest, "added": added if r.get("description_changed") else "",
            "method": "retrieval (emails and documents searched directly)"
                      if (r.get("prompt_version") or "").endswith("retrieve") else "thread summaries",
            "excerpts": src.get("email_excerpts", []),
            "notAdded": src.get("not_added", []),
            "found": {"history": src.get("history_added"), "steps": src.get("steps_added"),
                      "done": src.get("steps_done")},
            "changed": bool(r.get("description_changed")),
            "history": r.get("history_append") or "", "next": r.get("proposed_next") or "",
            "from": "deal summary", "review": 1 if r.get("review_status") == "needs_review" else 0,
            "emails": src.get("emails", 0), "documents": src.get("documents", []),
            "flags": [f.get("claim", "") if f.get("check") == "notion_outdated"
                      else f"{f.get('check')}: {f.get('claim')}"
                      for f in _json_list(r.get("verification")) if f.get("review")],
            "created": (r.get("created_at") or "")[:16].replace("T", " "),
            # the user's approval, valid only for the proposal version it was given to
            "approval": ({"by": r["_approval"]["approved_by"], "at": r["_approval"]["approved_at"],
                          "checked": bool(r["_approval"].get("after_checking")),
                          "current": r["_approval"]["proposal_created"] == r.get("created_at")}
                         if r.get("_approval") else None)}


def summariser_note(cfg):
    """The note generate.py keeps while it works: (note, running?)."""
    path = cfg["_data_dir"] / "logs" / "generate_progress.json"
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(note["updated"])).total_seconds()
    except (KeyError, ValueError, TypeError):
        age = 10 ** 9
    return note, (not note.get("stopped") and age < STALE_AFTER)


def thread_status(conv, t, note, running):
    """not_run | running | partial | done, with a short label."""
    cmd = f"generate.py --thread {conv[:12]}"
    if note and note.get("conversation_id") == conv and running:
        return {"state": "running", "label": f"In progress: part {note.get('calls_done', 0) + 1} "
                f"of {max(note.get('calls_total', 1), note.get('calls_done', 0) + 1)}", "cmd": cmd}
    if t["generated"] and t["generated"] >= t["count"] and t["result"]:
        return {"state": "done", "label": "Done", "cmd": cmd}
    if t["generated"] or (note and note.get("conversation_id") == conv):
        return {"state": "partial", "label": f"Interrupted: {t['generated']} of {t['count']} "
                f"email(s) summarised", "cmd": cmd}
    return {"state": "not_run", "label": "Not run yet", "cmd": cmd}


def main():
    ap = argparse.ArgumentParser(description="Build the deal-centred page.")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    ap.add_argument("--all", action="store_true",
                    help="list every deal (default: deals with emails, then live deals)")
    args = ap.parse_args()
    out = build(load_config(), include_all=args.all)
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


def build(cfg, include_all=False, quiet=False):
    """Write <data folder>/deals.html and return its path."""
    say = (lambda *a: None) if quiet else print
    ndir = cfg["_data_dir"] / "notion"
    if not (ndir / "directory.json").exists():
        sys.exit("No Notion directory yet. Run notion_sync.py first.")
    d = json.loads((ndir / "directory.json").read_text(encoding="utf-8"))
    matches = json.loads((ndir / "matches.json").read_text(encoding="utf-8")) \
        if (ndir / "matches.json").exists() else []
    if not matches:
        say("Note: no matches.json yet (run notion_match.py): deals will show no emails.")

    projects = {p["id"]: p for p in d["projects"]}
    cps = {c["id"]: c for c in d["counterparties"]}
    contacts_of_cp = defaultdict(list)
    for c in d["contacts"]:
        for cid in c["organisations"]:
            contacts_of_cp[cid].append(c)

    threads = email_threads(cfg["_data_dir"] / "db" / "proparco.db")
    try:
        full = {t["id"]: t for t in viewer.collect(cfg)}
    except Exception as err:                      # fall back to the database (no attachment links)
        say(f"Note: attachment links unavailable ({err.__class__.__name__}: {str(err)[:120]}); "
              "emails are shown from the database. Update viewer.py on this drive.")
        full = {}
    deal_threads, unmatched = defaultdict(list), []
    for m in matches:
        t = threads.get(m["conversation_id"])
        if t is None:
            continue
        if not m["candidates"]:
            unmatched.append((m, t))
        top = m["candidates"][0]["score"] if m["candidates"] else 0
        for rank, c in enumerate(m["candidates"]):
            if c["project_id"] in projects:
                deal_threads[c["project_id"]].append({
                    "m": m, "t": t, "full": full.get(m["conversation_id"]),
                    "top": m["candidates"][0],
                    "score": c["score"], "evidence": c["evidence"],
                    "best": rank == 0 or c["score"] >= top})

    note, running = summariser_note(cfg)
    deal_props = {}
    try:                                   # deal-level proposals (deal_summarize.py)
        pdb = read_db(cfg["_data_dir"] / "db" / "proparco.db")
        if pdb.execute("SELECT 1 FROM sqlite_master WHERE name = 'deal_proposals'").fetchone():
            deal_props = {r["project_id"]: dict(r) for r in pdb.execute("SELECT * FROM deal_proposals")}
            if pdb.execute("SELECT 1 FROM sqlite_master WHERE name = 'deal_approvals'").fetchone():
                for a in pdb.execute("SELECT * FROM deal_approvals"):
                    if a["project_id"] in deal_props:
                        deal_props[a["project_id"]]["_approval"] = dict(a)
        pdb.close()
    except sqlite3.Error:
        pass
    any_running = False
    from sources import server_databases
    sdbs = []                                            # every sector database on this laptop
    for label, path in server_databases(cfg):
        try:
            sdbs.append((label, read_db(path)))
        except sqlite3.Error:
            continue

    with_emails = [pid for pid in projects if any(x["best"] for x in deal_threads.get(pid, []))]
    live = [pid for pid, p in projects.items()
            if pid not in with_emails and not any(w in (p["status"] or "").lower() for w in INACTIVE)]
    rest = [pid for pid in projects if pid not in with_emails and pid not in live]
    order = sorted(with_emails, key=lambda pid: max(x["t"]["last"] for x in deal_threads[pid] if x["best"]),
                   reverse=True) + sorted(live, key=lambda pid: projects[pid]["edited"], reverse=True)
    if include_all:
        order += sorted(rest, key=lambda pid: projects[pid]["name"].lower())

    items = []
    for pid in order:
        p = projects[pid]
        # the deal's own client(s): counterparties listing this deal among their related projects
        clients = [c for c in p["counterparties"] if c in cps and pid in cps[c]["projects"]] \
            or [c for c in p["counterparties"] if c in cps][:1]
        names = sorted({core_name(p["name"])} | {cps[c]["name"] for c in clients})
        dts = sorted(deal_threads.get(pid, []), key=lambda x: x["t"]["last"], reverse=True)   # newest first
        dts.sort(key=lambda x: not x["best"])                                                 # best matches first (stable)
        proposal = next((x for x in dts if x["t"]["result"] and x["best"]), None)
        for x in dts:
            x["status"] = thread_status(x["m"]["conversation_id"], x["t"], note, running)
            if pid in deal_props and x["status"]["state"] == "not_run":
                x["status"] = {"state": "deal", "label": "Covered by the deal summary above"}
        best = [x for x in dts if x["best"]]
        states = [x["status"]["state"] for x in best]
        if dts and not best:
            agent = {"state": "alternative", "label": "No agent summary for this deal",
                     "other": dts[0]["top"]["deal"], "otherId": dts[0]["top"]["project_id"]}
        elif "running" in states:
            agent = next(x["status"] for x in best if x["status"]["state"] == "running")
            any_running = True
        elif proposal is not None and states and all(st == "done" for st in states):
            agent = {"state": "done", "label": "Done"}
        elif proposal is not None or "partial" in states:
            agent = next((x["status"] for x in best if x["status"]["state"] == "partial"),
                         {"state": "done", "label": "Done"})
        elif best:
            agent = best[0]["status"]
        else:
            agent = {"state": "none", "label": "No email threads for this deal"}
        items.append({
            "id": pid, "name": p["name"], "status": p["status"], "team": p["team"],
            "countries": p["countries"], "url": p["url"], "projectId": p["project_id"],
            "counterparties": [cps[c]["name"] for c in p["counterparties"] if c in cps],
            "contacts": [{"name": c["name"], "email": c["email"], "job": c["job"]}
                         for cid in clients + [x for x in p["counterparties"] if x not in clients]
                         for c in contacts_of_cp.get(cid, [])][:8],
            "notion": {"latest": p.get("latest", ""), "history": p.get("history", ""),
                       "next": p.get("next", "")},
            "agent": agent if pid not in deal_props or agent.get("state") == "running"
                     else {"state": "done", "label": "Done"},
            "proposal": deal_proposal(deal_props[pid]) if pid in deal_props else
                        None if proposal is None else {
                "latest": proposal["t"]["result"].get("latest_deal_summary") or "",
                "history": proposal["t"]["result"].get("project_history") or "",
                "next": proposal["t"]["result"].get("next_steps") or "",
                "from": proposal["t"]["subject"], "review": proposal["t"]["review"]},
            "threads": [{"subject": x["t"]["subject"], "last": x["t"]["last"][:10],
                         "count": x["t"]["count"], "senders": x["t"]["senders"][:4],
                         "score": x["score"], "evidence": x["evidence"], "best": x["best"],
                         "agent": {"latest": (x["t"]["result"] or {}).get("latest_deal_summary") or "",
                                   "history": (x["t"]["result"] or {}).get("project_history") or "",
                                   "next": (x["t"]["result"] or {}).get("next_steps") or ""},
                         "review": x["t"]["review"], "status": x["status"],
                         "topId": x["top"]["project_id"], "topName": x["top"]["deal"],
                         "sharedWith": [c["deal"] for c in x["m"]["candidates"]
                                        if c["score"] >= x["m"]["candidates"][0]["score"] and c["project_id"] != pid],
                         **page_emails((x["full"] or {}).get("emails") or x["t"]["emails"])}
                        for x in dts],
            "docs": all_related_documents(cfg, sdbs, names) if (dts or len(order) <= 150) else [],
            "hasEmails": any(x["best"] for x in dts),
            "hasSummary": pid in deal_props,
            "summaryChanged": bool(pid in deal_props and (deal_props[pid].get("description_changed")
                                   or (deal_props[pid].get("history_append") or "").strip()
                                   or "— awaiting" in (deal_props[pid].get("proposed_next") or "")
                                   or json.loads(deal_props[pid].get("sources_json") or "{}").get("steps_added"))),
        })
    for _, con in sdbs:
        con.close()
    loose = [{"subject": t["subject"], "last": t["last"][:10], "count": t["count"],
              "senders": t["senders"][:3], "unknown": m.get("unknown_addresses", [])[:4]}
             for m, t in sorted(unmatched, key=lambda mt: mt[1]["last"], reverse=True)]

    extracts = None
    try:                                   # the Info list: from the export snapshot, if one was taken
        import eval_view as EV
        snap = EV.load_snapshot(cfg)
        if snap:
            allm = EV.mail_scope(cfg) == "all"
            extracts = {"made": snap.get("made", ""), "all": allm,
                        "mail": [{"name": m["name"], "emails": m.get("exported"), "complete": str(m.get("verdict", "")).startswith("ALL"),
                                  "error": bool(m.get("error"))} for m in snap.get("mail", []) if allm or EV.is_own(cfg, m["path"])],
                        "hidden": sum(1 for m in snap.get("mail", []) if not (allm or EV.is_own(cfg, m["path"]))),
                        "sectors": [{"name": x["name"], "docs": x.get("docs"), "indexed": bool(x.get("indexed"))} for x in snap.get("sectors", [])]}
    except Exception:
        extracts = None
    try:
        who = mailbox_label(cfg)
    except Exception:
        who = {"name": cfg["_data_dir"].name, "email": ""}
    who_html = (f'<span class="who" title="The mailbox this page shows (set when the app starts)">'
                f'<span class="dot"></span>Mailbox: {html.escape(who["name"])}'
                + (f' · {html.escape(who["email"])}' if who["email"] else "") + "</span>")
    page = PAGE.replace("__WHO__", who_html).replace("__DATA__", json.dumps({"deals": items, "loose": loose, "maxOpen": MAX_OPEN_THREADS, "extracts": extracts}, ensure_ascii=False)
                        .replace("</", "<\\/"))
    page = page.replace("__BUILT__", esc(f"Built {datetime.now():%d/%m/%Y %H:%M} · Notion synced "
                                         f"{d.get('synced_at', '?')[:16].replace('T', ' ')} · "
                                         f"{len(with_emails)} deals with emails · read-only"))
    if any_running:   # the open page reloads itself while the summariser works
        page = page.replace("<title>", '<meta http-equiv="refresh" content="30"><title>', 1)
    out = cfg["_data_dir"] / "deals.html"
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, out)
    say(f"Deal view written: {out}")
    say(f"   {len(with_emails)} deals with matched emails, {len(live)} other live deals"
        f"{'' if not include_all else f', {len(rest)} closed or dropped'}; "
        f"{len(loose)} email threads without a deal")
    return out


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deals: emails and documents</title>
<style>
:root{--bg:#f3f6f7;--panel:#fff;--ink:#1d2b33;--muted:#5d6b73;--line:#d9e1e5;--accent:#0f5c6e;
--good:#1f7a4d;--warn:#a35a00;--chip:#e6eef1}
@media (prefers-color-scheme:dark){:root{--bg:#12181b;--panel:#1a2226;--ink:#e4eaed;--muted:#9aa8af;
--line:#2c383e;--accent:#6cc3d6;--good:#6fd29c;--warn:#f0b35a;--chip:#24313a}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;
background:var(--bg);color:var(--ink)}
html,body{height:100%}body{display:flex;flex-direction:column}
header{padding:24px 20px 12px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header .spacer{flex:1}
.who{display:inline-flex;align-items:center;gap:7px;background:#fff;color:#1d2b33;border:1px solid #cfd8dc;border-radius:999px;padding:4px 12px;font-size:13px;font-weight:600;cursor:default;box-shadow:0 1px 2px rgba(0,0,0,.06)}
.who .dot{width:8px;height:8px;border-radius:50%;background:#2f9e44}.menubtn{font:inherit;font-size:20px;line-height:1;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:8px;padding:5px 10px;cursor:pointer}
h1{font-size:19px;margin:0}input[type=search]{padding:7px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--panel);color:var(--ink);min-width:260px}
label{color:var(--muted);font-size:14px}.built{color:var(--muted);font-size:13px}
main{display:grid;grid-template-columns:340px 1fr;flex:1;min-height:0;transition:grid-template-columns .2s ease}
body.collapsed main{grid-template-columns:0 1fr}body.collapsed #side{visibility:hidden}
#side{min-height:0;border-right:1px solid var(--line);overflow-y:auto}
details.sec2{border-bottom:1px solid var(--line)}details.sec2>summary{cursor:pointer;padding:10px 14px;font-weight:700;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);list-style-position:inside}
details.sec2[open]>summary{border-bottom:1px solid var(--line)}
#side .nav2 .evp{font-weight:400;padding-left:26px;font-size:14px}
#info{padding:6px 0 10px}#info .ih{padding:8px 14px 4px;font-size:12.5px;font-weight:700;color:var(--muted)}
#info button{display:block;width:100%;text-align:left;padding:6px 14px;border:0;background:transparent;color:var(--ink);font:inherit;font-size:14px;cursor:pointer}
#info button:hover{background:var(--chip)}#info .m2{color:var(--muted);font-size:12.5px}#info .ok2{color:#2f9e44}#info .fl2{color:#c2410c}#info .note{padding:4px 14px;font-size:12.5px;color:var(--muted)}
#side .filters{padding:12px 14px;display:flex;flex-direction:column;gap:8px}
#side .filters input[type=search]{min-width:0;width:100%}#side .filters label{display:flex;flex-direction:column;gap:3px}
#side .filters select{width:100%}#side .nav2 button{display:block;width:100%;text-align:left;padding:9px 14px;border:0;border-bottom:1px solid var(--line);background:transparent;color:var(--ink);cursor:pointer;font:inherit;font-weight:600}
#side .nav2 button[aria-current=true]{background:var(--chip);box-shadow:inset 3px 0 var(--accent)}
nav{overflow:visible}
#detail iframe.evalframe{width:100%;height:100%;border:0;display:block}#detail.fill{padding:0;overflow:hidden}nav button{display:block;width:100%;text-align:left;
padding:10px 14px;border:0;border-bottom:1px solid var(--line);background:transparent;color:var(--ink);cursor:pointer}
nav button[aria-current=true]{background:var(--chip);box-shadow:inset 3px 0 var(--accent)}
nav .n{font-weight:600}nav .m{font-size:13px;color:var(--muted)}
.badge{display:inline-block;font-size:12px;padding:0 7px;border-radius:9px;border:1px solid var(--line);margin-left:4px}
.mail{color:var(--good);border-color:var(--good)}.run{color:var(--accent);border-color:var(--accent)}
code{font-size:13px;background:var(--chip);padding:1px 5px;border-radius:4px}.rev{color:var(--warn);border-color:var(--warn)}
section{overflow:auto;padding:18px 26px}h2{margin:0 0 4px;font-size:21px}h3{margin:22px 0 8px;font-size:16px}
.meta{color:var(--muted);font-size:14px}a{color:var(--accent)}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:8px}
td,th{border-bottom:1px solid var(--line);padding:9px 11px;vertical-align:top;text-align:left}
th{font-size:13px;color:var(--muted);font-weight:600;width:16%}td{width:42%}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 13px;margin:8px 0}
.none{color:var(--muted);font-style:italic}.why{font-size:13px;color:var(--muted)}
.wrap{overflow-x:auto}.att{margin:6px 0;padding-left:18px}summary{cursor:pointer;color:var(--accent)}
.dealhead{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}
.notion-btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:9px;
border:1px solid var(--accent);color:var(--accent);text-decoration:none;font-weight:600;font-size:14px;white-space:nowrap}
.notion-btn:hover{background:var(--accent);color:var(--panel)}
.facts{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0 6px}
.fact{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:7px 12px;min-width:120px}
.fact .k{display:block;font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:700;margin-bottom:2px}
.fact .v{font-size:14.5px;font-weight:600}
.fact.stage{border-left:5px solid var(--stc,var(--line))}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.02em}
.unset{color:var(--muted);font-style:italic;font-weight:400}
.pill{display:inline-block;font-size:13px;font-weight:600;padding:1px 10px;border-radius:999px;background:var(--chip);margin:1px 5px 1px 0}
.cps{margin:8px 0 2px}.cps .k{font-size:11px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:700;margin-right:6px}
.stage-early{--stc:#d98a00}.stage-loi{--stc:#2f6fd6}.stage-formal{--stc:#0f8a9c}.stage-signed{--stc:#1f9a5a}
.stage-lead{--stc:#8a5ad6}.stage-inactive{--stc:#9aa8af}
@media (prefers-color-scheme:dark){.stage-early{--stc:#f0b35a}.stage-loi{--stc:#7fa8ff}.stage-formal{--stc:#6cc3d6}
.stage-signed{--stc:#6fd29c}.stage-lead{--stc:#b99cff}}
#chatpanel{position:fixed;top:0;right:0;height:100%;width:min(820px,96vw);background:var(--bg,#fff);box-shadow:-6px 0 24px rgba(0,0,0,.25);
  transform:translateX(105%);transition:transform .2s ease;z-index:50;display:flex;flex-direction:column}
#chatpanel.open{transform:none}#chatpanel .bar{display:flex;align-items:center;gap:12px;padding:8px 12px;border-bottom:1px solid #8884}
#chatpanel .bar b{flex:1}#chatpanel iframe{flex:1;border:0;width:100%}#chatpanel .x{cursor:pointer;font-size:20px;line-height:1;padding:0 6px}
.chatbtn{display:inline-block;margin-left:10px;padding:4px 12px;border-radius:8px;border:1px solid currentColor;text-decoration:none;font-weight:600;font-size:13px}
details.sec{margin-top:14px;border-top:1px solid #8883;padding-top:8px}details.sec>summary{font-weight:700;font-size:15px;cursor:pointer;list-style-position:outside}details.sec[open]>summary{margin-bottom:6px}
.upd .verdict{margin:6px 0 8px;font-weight:600}.upd.ready .verdict{color:#2f9e44}.upd.wait .verdict{color:#e8590c}.upd.none .verdict{color:#777}.updt{width:100%;border-collapse:collapse;font-size:13.5px}.updt th,.updt td{vertical-align:top;text-align:left;padding:6px;border-top:1px solid #8883}.updt th:first-child{width:150px}.updt mark{background:#d3f9d8;color:inherit;padding:0 2px;border-radius:3px}@media (prefers-color-scheme:dark){.updt mark{background:#1f4d2b}}.updt a.copy{font-size:12px;font-weight:600}
.upd .updtitle{display:inline-block;background:#fff4e6;color:#d9480f;border:1px solid #ffa94d;border-radius:8px;padding:3px 10px}@media (prefers-color-scheme:dark){.upd .updtitle{background:#3b2410;color:#ffa94d}}.upd .action{margin:8px 0 10px;display:flex;flex-wrap:wrap;gap:10px;align-items:center}button.approve{font:inherit;font-weight:700;border:0;border-radius:9px;padding:8px 16px;background:#2f9e44;color:#fff;cursor:pointer}button.approve:disabled{background:#adb5bd;cursor:default}.upd .approved{color:#2f9e44;font-weight:600}.upd .approved a{margin-left:8px;font-weight:400}
.approvebtn{display:inline-block;font:inherit;font-weight:600;font-size:13px;padding:4px 12px;border-radius:8px;border:1px solid #2f9e44;background:transparent;color:#2f9e44;cursor:pointer}.approvebtn:hover:not(:disabled){background:#2f9e44;color:#fff}.approvebtn:disabled{border-color:#adb5bd;color:#adb5bd;cursor:default}.upd .chk{display:flex;gap:6px;align-items:center;font-size:13px}
</style></head><body>
<header><button id="menuBtn" class="menubtn" title="Show or hide the deal list" aria-label="Menu">&#9776;</button>
<h1>Deals: emails and documents</h1>__WHO__<span class="spacer"></span>
<a id="chatLink" class="chatbtn" href="http://127.0.0.1:8765/" title="Ask questions across emails and documents">Chat &rarr;</a>
</header>
<main><aside id="side">
<details class="sec2" id="secFilters" open><summary>Filters</summary>
<div class="filters">
<input type="search" id="q" placeholder="Search: deal, project ID, counterparty, contact…">
<label>Show <select id="fShow" autocomplete="off"><option value="summary">deals with an agent summary</option>
<option value="mail">deals with emails</option><option value="all">all deals</option></select></label>
<label>Sector <select id="fSector" autocomplete="off"><option value="">all sectors</option></select></label>
<label>Projects <select id="fProj" autocomplete="off"><option value="">all deals</option>
<option value="with">with a project ID</option><option value="without">without a project ID</option></select></label>
<span class="meta" id="count"></span><div class="built">__BUILT__</div>
</div></details>
<details class="sec2" id="secDeals" open><summary>Deals</summary><nav id="list"></nav></details>
<details class="sec2" id="secEval" style="display:none"><summary>Evaluation</summary>
<div class="nav2"><button id="evalNav" aria-current="false">Open the evaluation</button>
<button class="evp" data-p="p1">Search quality</button><button class="evp" data-p="p2">Deal summaries</button>
<button class="evp" data-p="p3">Approvals</button><button class="evp" data-p="p4">Chat answers</button>
<button class="evp" data-p="p5">Coverage</button><button class="evp" data-p="p6">Email exports</button>
<button class="evp" data-p="p7">Server extraction</button></div></details>
<details class="sec2" id="secInfo"><summary>Info: extracts</summary><div id="info"></div></details>
</aside><section id="detail"></section></main>
<script>
const DATA = __DATA__;
const $ = id => document.getElementById(id);
// served by the chat program (http): links stay on the same address; opened as a file: the chat's usual address
const CHAT = location.protocol.startsWith('http') ? '/' : 'http://127.0.0.1:8765/';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// ---- "Can we update Notion now?": Notion now, the proposed record after the update, and what changes
function notionUpdate(d, p) {
  if (!p || p.kind !== 'deal') return '';
  const n = d.notion || {}, norm = x => (x || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const lines = x => (x || '').split('\n').map(l => l.replace(/^\s*[-•]\s*/, '').trim()).filter(Boolean);
  // 1. latest deal description: Notion's text kept word for word, the new facts appended
  const latestNew = (n.latest || '').trim() && p.added ? esc(n.latest.trim()) + ' <mark>' + esc(p.added) + '</mark>' : (p.added ? '<mark>' + esc(p.latest) + '</mark>' : para(n.latest));
  const latestChange = p.added ? 'Updated: new facts added at the end' : 'No change';
  // 2. history: Notion's history kept, the agent's dated entries appended
  const hAdd = lines(p.history);
  const histNew = (n.history || '').trim() || !hAdd.length
    ? [para(n.history), ...hAdd.map(h => '<mark>' + esc(h) + '</mark>')].join('<br>')
    : hAdd.map(h => '<mark>' + esc(h) + '</mark>').join('<br>');
  const histChange = hAdd.length ? `Added: ${hAdd.length} dated entr${hAdd.length > 1 ? 'ies' : 'y'}` : 'No change';
  // 3. next steps: Notion's steps kept (done ones ticked), new ones added
  const noStep = x => /^no (outstanding|open|further|new) (actions?|steps?)/i.test(x.replace(/^[-•✓]\s*/, ''));
  const nOld = lines(n.next).filter(x => !noStep(x)).map(norm), steps = lines(p.next).filter(x => !noStep(x));
  const done = steps.filter(x => x.startsWith('✓')).length;
  const added = steps.filter(x => !x.startsWith('✓') && !nOld.includes(norm(x.split(' — ')[0])));
  const stepsNew = steps.map(x => x.startsWith('✓') ? '<s>' + esc(x.replace(/^✓\s*/, '')) + '</s> ✓ done'
                                   : (added.includes(x) ? '<mark>' + esc(x) + '</mark>' : esc(x))).join('<br>') || para('');
  const stepsChange = [done ? `${done} marked done` : '', added.length ? `${added.length} added` : ''].filter(Boolean).join(', ') || 'No change';
  const changed = !!(p.added || hAdd.length || done || added.length);
  const verdict = !changed ? ['none', 'Nothing to update: Notion already reflects what the emails and documents say.']
    : (p.review || (p.flags && p.flags.length)) ? ['wait', 'Not yet: check the flagged item(s) below first' + (p.flags && p.flags.length ? ': ' + p.flags.map(esc).join(' · ') : '.')]
    : ['ready', 'Yes, ready: the changes below are supported by the cited emails and documents.'];
  // plain text after the update, for copying into Notion by hand until the automatic push exists
  const txt = {
    latest: ((n.latest || '').trim() + (p.added ? ' ' + p.added : '')).trim(),
    history: [(n.history || '').trim(), ...hAdd].filter(Boolean).join('\n'),
    next: steps.map(x => x.startsWith('✓') ? x.replace(/^✓\s*/, '') + ' (done)' : x.replace(/^[-•]\s*/, '')).join('\n'),
  };
  window.__copy = window.__copy || {}; window.__copy[d.id] = txt;
  const row = (label, now, after, change, key) => `<tr><th>${label}</th><td>${now}</td><td>${after}</td>
     <td>${esc(change)}${change !== 'No change' ? `<div><a href="#" class="copy" data-deal="${esc(d.id)}" data-k="${key}">Copy</a></div>` : ''}</td></tr>`;
  const live = location.protocol.startsWith('http'), ap = p.approval;
  let action;
  if (ap && ap.current) {
    action = `<div class="approved">✓ Approved by ${esc(ap.by)} on ${esc(ap.at)}${ap.checked ? ', after checking the flagged items' : ''} · not sent to Notion yet: the push will send approved updates only.
      <a href="#" class="apv" data-deal="${esc(d.id)}" data-action="undo">Undo</a></div>`;
  } else if (verdict[0] === 'ready') {
    action = `<button class="apv approvebtn" data-deal="${esc(d.id)}" data-action="approve" ${live ? '' : 'disabled title="Open the deal page from the app to approve"'}>Approve this update &#10003;</button>
      <span class="meta">The changes below are supported by the cited emails and documents.</span>` +
      (ap ? `<div class="meta">An earlier version of this proposal was approved by ${esc(ap.by)} on ${esc(ap.at)}; this one is new.</div>` : '');
  } else if (verdict[0] === 'wait') {
    action = `<div class="verdict">${verdict[1]}</div>
      <label class="chk"><input type="checkbox" class="checked-box" data-deal="${esc(d.id)}" ${live ? '' : 'disabled'}>
        I have checked the flagged item(s) against the sources</label>
      <button class="apv approvebtn" data-deal="${esc(d.id)}" data-action="approve" data-checked="1" disabled
        title="Tick the box once you have checked the flagged items">Approve this update &#10003;</button>` +
      (ap ? `<div class="meta">An earlier version of this proposal was approved by ${esc(ap.by)} on ${esc(ap.at)}; this one is new.</div>` : '');
  } else {
    action = `<div class="verdict">${verdict[1]}</div>`;
  }
  return `<div class="card upd ${verdict[0]}"><strong class="updtitle">Can we update Notion now?</strong>
    <div class="action">${action}</div>
    <table class="updt"><tr><th></th><th>In Notion now</th><th>After the update <span class="meta">(added parts highlighted)</span></th><th>What changes</th></tr>
    ${row('Latest deal description', para(n.latest), latestNew, latestChange, 'latest')}
    ${row('History of discussions', para(n.history), histNew || para(''), histChange, 'history')}
    ${row('Next steps', para(n.next), stepsNew, stepsChange, 'next')}</table>
    <p class="meta">Nothing is sent to Notion: the automatic update comes next, with approval per field. Until then, "Copy" gives the text after the update, to paste into Notion by hand.</p></div>`;
}
document.addEventListener('click', async e => {
  const b = e.target.closest('.apv'); if (!b) return; e.preventDefault();
  if (b.dataset.action === 'approve' && !confirm('Approve this update? It is recorded on this laptop; nothing is sent to Notion yet.')) return;
  const q = {deal: b.dataset.deal, action: b.dataset.action}; if (b.dataset.checked) q.checked = '1';
  const r = await (await fetch('/api/approve?' + new URLSearchParams(q))).json();
  if (!r.ok) { alert(r.message); return; }
  // update only this deal, in place: no reload, no rebuild, the reader stays where they are
  const d = DATA.deals.find(x => x.id === b.dataset.deal);
  if (d && d.proposal) {
    d.proposal.approval = b.dataset.action === 'undo' ? null : {by: r.by, at: r.at, checked: !!r.checked, current: true};
    const box = $('detail'), y = box.scrollTop, wy = window.scrollY;
    show(d.id);
    box.scrollTop = y; window.scrollTo(0, wy);
  } else location.reload();
});
document.addEventListener('change', e => {
  const c = e.target.closest('.checked-box'); if (!c) return;
  const b = c.closest('.action').querySelector('.approvebtn'); if (b) b.disabled = !c.checked;
});
document.addEventListener('click', e => {
  const a = e.target.closest('a.copy'); if (!a) return; e.preventDefault();
  const t = ((window.__copy || {})[a.dataset.deal] || {})[a.dataset.k] || '';
  const done = () => { a.textContent = 'Copied'; setTimeout(() => a.textContent = 'Copy', 1500); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(done, () => { prompt('Copy this text:', t); });
  else prompt('Copy this text:', t);
});
const para = s => (s && s.trim()) ? esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>') : '<span class="none">(empty)</span>';
let current = null;
function items() {
  const q = $('q').value.toLowerCase().trim();
  const mode = $('fShow').value, sector = $('fSector').value;
  const inMode = d => mode === 'all' || (mode === 'mail' ? d.hasEmails : d.hasSummary);
  // sector options: counted within the current "Show" choice, so the numbers match the list
  const counts = {};
  DATA.deals.filter(inMode).forEach(d => (d.team.length ? d.team : ['(not set)']).forEach(t => counts[t] = (counts[t] || 0) + 1));
  const sel = $('fSector'), keep = sel.value;
  sel.innerHTML = `<option value="">all sectors (${DATA.deals.filter(inMode).length})</option>` +
    Object.keys(counts).sort().map(t => `<option value="${esc(t)}">${esc(t)} (${counts[t]})</option>`).join('');
  sel.value = counts[keep] ? keep : '';
  const proj = $('fProj').value, hasPid = d => !!(d.projectId && String(d.projectId).trim());
  const list = DATA.deals.filter(d => inMode(d) && (!sel.value || (d.team.length ? d.team : ['(not set)']).includes(sel.value))
      && (!proj || (proj === 'with' ? hasPid(d) : !hasPid(d))))
    .sort((a, b) => (b.hasSummary - a.hasSummary) || (b.summaryChanged - a.summaryChanged));
  if (!q) return list;
  // every word typed must match: name, project ID, status, sector, country, counterparties, contacts,
  // email subjects and the agent summary
  const words = q.split(/\s+/).filter(Boolean);
  return list.filter(d => {
    const hay = [d.name, d.projectId, d.status, ...(d.team || []), ...(d.countries || []), ...d.counterparties,
      ...d.contacts.map(c => c.name + ' ' + c.email), ...d.threads.map(t => t.subject),
      d.proposal && d.proposal.latest].filter(Boolean).join(' ').toLowerCase();
    return words.every(w => hay.includes(w));
  });
}
function renderList() {
  const shown = items();
  $('count').textContent = `${shown.length} deals` + (DATA.loose.length ? ` · ${DATA.loose.length} emails without a deal` : '');
  $('list').innerHTML = shown.map(d => `<button data-id="${esc(d.id)}" aria-current="${d.id === current}">
    <div class="n">${esc(d.name)}</div><div class="m">${esc(d.status || 'no status')}
    ${d.threads.filter(t => t.best).length ? `<span class="badge mail">${d.threads.filter(t => t.best).length} email thread${d.threads.filter(t => t.best).length > 1 ? 's' : ''}</span>` : ''}
    ${d.threads.some(t => t.best && t.review) ? '<span class="badge rev">needs review</span>' : ''}
    ${d.agent && d.agent.state === 'running' ? '<span class="badge run">summarising…</span>' : ''}</div></button>`).join('')
    + (DATA.loose.length ? `<button data-id="__loose" aria-current="${current === '__loose'}"><div class="n">Emails without a deal</div>
       <div class="m">${DATA.loose.length} thread(s): attach later</div></button>` : '');
  $('list').querySelectorAll('button').forEach(b => b.onclick = () => show(b.dataset.id));
  if (current === '__eval') return;          // the Evaluation view stays open while filtering
  if (current) show(current); else if (shown.length) show(shown[0].id);
}
function statusHtml(st) {
  // Status only: no commands on the page. The command to run is in the tooltip (for the operator).
  const tip = st.cmd ? ` title="To generate: start.bat ${esc(st.cmd)}  (Linux: sh start.sh ${esc(st.cmd)})"` : '';
  if (st.state === 'running') return `<span class="badge run">${esc(st.label)}</span><div class="why">The agent is working on this now; this page updates itself.</div>`;
  if (st.state === 'partial') return `<span class="badge rev"${tip}>Agent summary incomplete</span><div class="why">Generation was interrupted before the end.</div>`;
  if (st.state === 'not_run') return `<span class="badge"${tip}>Agent summary not generated yet</span>`;
  if (st.state === 'none') return '<span class="none">No email threads for this deal yet.</span>';
  if (st.state === 'deal') return '<span class="badge">Covered by the deal summary above</span>';
  if (st.state === 'alternative') return `<span class="badge">No agent summary for this deal</span>
    <div class="why">Its only email thread is more likely about <a href="#${encodeURIComponent(st.otherId)}" onclick="show('${esc(st.otherId)}');return false">${esc(st.other)}</a>.</div>`;
  return '';
}
function attList(a) {
  if (!a.length) return '';
  return '<ul class="att">' + a.map(x => x.href
    ? `<li><a href="${esc(x.href)}" target="_blank" rel="noopener">${esc(x.name)}</a>${x.sizeMB ? ` <span class="meta">${x.sizeMB} MB</span>` : ''}</li>`
    : `<li>${esc(x.name)} <span class="meta">(${esc(x.reason || 'not saved')})</span></li>`).join('') + '</ul>';
}
function stageClass(status) {
  const s = (status || '').toLowerCase();
  if (s.includes('non active')) return 'stage-inactive';
  if (s.includes('signed') && !s.includes('loi')) return 'stage-signed';
  if (s.includes('formalisation') || s.includes('authori')) return 'stage-formal';
  if (s.includes('loi') || s.includes('instruction') || s.includes('post cid')) return 'stage-loi';
  if (s.includes('early') || s.includes('pipe') || s.includes('pre cid')) return 'stage-early';
  if (s.includes('lead') || s.includes('target')) return 'stage-lead';
  return '';
}
const pills = xs => xs.map(x => `<span class="pill">${esc(x)}</span>`).join('');
function threadCard(t) {
  if (!t.best) {      // alternative match: the thread belongs more likely to another deal
    return `<div class="card"><strong>${esc(t.subject)}</strong>
      <span class="meta">${esc(t.last)} · ${t.count} email(s) · possible match ${t.score.toFixed(1)}</span>
      <div class="why">More likely about <a href="#${encodeURIComponent(t.topId)}" onclick="show('${esc(t.topId)}');return false">${esc(t.topName)}</a>; shown here because ${esc(t.evidence.join('; '))}.</div></div>`;
  }
  const has = t.agent.latest || t.agent.history || t.agent.next;
  const nAtt = t.emails.reduce((n, e) => n + e.attachments.length, 0);
  return `<div class="card"><strong>${esc(t.subject)}</strong>
    <span class="meta">${esc(t.last)} · ${t.count} email(s)${nAtt ? ` · ${nAtt} attachment(s)` : ''} · match ${t.score.toFixed(1)}${t.best ? '' : ' (alternative)'}</span>
    <div class="why">Why this deal: ${esc(t.evidence.join('; '))}</div>
    ${t.sharedWith && t.sharedWith.length ? `<div class="why">Matched equally to ${t.sharedWith.length} other deal(s): ${esc(t.sharedWith.slice(0, 4).join(', '))}${t.sharedWith.length > 4 ? '…' : ''}</div>` : ''}
    ${t.status && t.status.state !== 'done' ? `<div style="margin-top:6px">${statusHtml(t.status)}</div>` : ''}
    ${has ? `<h4 style="margin:10px 0 4px">Agent summary</h4>
      <table><tr><th>Latest deal</th><td>${para(t.agent.latest)}</td></tr>
      <tr><th>History</th><td>${para(t.agent.history)}</td></tr>
      <tr><th>Next steps</th><td>${para(t.agent.next)}</td></tr></table>` : ''}
    ${t.review ? '<div class="why" style="color:var(--warn)">Check before using: some names or numbers in this summary could not be matched word for word to the email (details under each email).</div>' : ''}
    ${t.emails.length ? `<details style="margin-top:8px"><summary>Show the ${t.olderEmails ? 'latest ' : ''}${t.emails.length} email(s) and attachments</summary>
      ${t.emails.map(e => `<div class="card"><strong>${esc(e.date)}</strong> · ${esc(e.subject)}
        <div class="why">From ${esc(e.from)}${e.to.length ? ' to ' + esc(e.to.join(', ')) : ''}</div>
        ${attList(e.attachments)}
        ${e.flags.length ? `<div class="why" style="color:var(--warn)">${e.flags.map(esc).join('<br>')}</div>` : ''}
        <details><summary>Email text</summary><div style="white-space:pre-wrap">${esc(e.body)}${e.bodyCut ? '\n… (shortened here; the full text is in the database and search)' : ''}</div></details></div>`).join('')}
      ${t.olderEmails ? `<p class="meta">${t.olderEmails} older email(s) in this thread not shown here (they are in the agent summary, the database and search).</p>` : ''}
      </details>` : ''}</div>`;
}
function show(id) {
  current = id; $('detail').classList.remove('fill');
  if ($('evalNav')) $('evalNav').setAttribute('aria-current', 'false');
  $('list').querySelectorAll('button').forEach(b => b.setAttribute('aria-current', b.dataset.id === id));
  if (id === '__loose') {
    $('detail').innerHTML = `<h2>Emails without a deal</h2><p class="meta">No contact, domain or name linked these threads to a Notion deal.</p>` +
      DATA.loose.map(t => `<div class="card"><strong>${esc(t.subject)}</strong> <span class="meta">${esc(t.last)} · ${t.count} email(s)</span>
       <div class="why">From: ${esc(t.senders.join(', '))}${t.unknown.length ? ' · not in Contacts: ' + esc(t.unknown.join(', ')) : ''}</div></div>`).join('');
    return;
  }
  const d = DATA.deals.find(x => x.id === id);
  if (!d) return;
  const p = d.proposal;
  const st = d.agent || {state: 'none'};
  const dealCard = (p && p.kind === 'deal') ? `<div class="card"><strong>Agent summary for the deal</strong>
      ${p.review ? ' <span class="badge rev">needs review</span>' : ' <span class="badge">proposal, not yet approved</span>'}
      ${(() => {
        const f = p.found || {}, n = (p.excerpts || []).length, parts = [];
        if (p.added) parts.push('a new fact for the description');
        if (f.history) parts.push(`${f.history} history entr${f.history > 1 ? 'ies' : 'y'}`);
        if (f.steps) parts.push(`${f.steps} new next step${f.steps > 1 ? 's' : ''}`);
        if (f.done) parts.push(`${f.done} Notion step${f.done > 1 ? 's' : ''} marked done`);
        return `<div class="why" style="margin-top:6px"><b>What the agent found:</b> ` +
          (parts.length ? parts.join(', ') + '.' : `nothing new beyond Notion's record`) +
          ` It read ${n || p.emails} email excerpt(s)${p.documents.length ? ' and ' + p.documents.length + ' document passage(s)' : ''}` +
          `${(p.notAdded || []).length ? `, and left out ${p.notAdded.length} item(s) it could not verify` : ''}.</div>` +
          (p.added ? `<div class="why"><b>New since Notion:</b> ${esc(p.added)}</div>` : '');
      })()}
      <table style="margin-top:6px"><tr><th>Latest deal</th><td>${para(p.latest)}</td></tr>
      <tr><th>History to add</th><td>${p.history ? para(p.history) : '<span class="none">No new entries</span>'}</td></tr>
      <tr><th>Next steps</th><td>${para(p.next)}</td></tr></table>
      ${p.flags && p.flags.length ? `<div class="why" style="color:var(--warn)">To check before using: ${p.flags.map(esc).join(' · ')}</div>` : ''}
      ${p.notAdded && p.notAdded.length ? `<details style="margin-top:6px"><summary>Not added: ${p.notAdded.length} item(s) the agent could not verify in the emails</summary>
        <table>${p.notAdded.map(x => `<tr><th>${esc(x.kind)}</th><td>${esc(x.text)}<div class="why">${esc(x.reason)}</div></td></tr>`).join('')}</table></details>` : ''}
      ${p.excerpts && p.excerpts.length ? `<details style="margin-top:6px"><summary>Sources: the ${p.excerpts.length} email excerpt(s) the labels refer to</summary>
        <table>${p.excerpts.map(e => `<tr><th>${esc(e.label)}</th><td>${esc(e.date)} · ${esc(e.from)} · ${esc(e.subject)}</td></tr>`).join('')}</table></details>` : ''}
      ${(() => {
        const files = new Map(); (p.documents || []).forEach(x => { const k = x.file; files.set(k, (files.get(k) || []).concat(x.label)); });
        return `<details style="margin-top:6px"><summary>How this summary was made</summary><table>
          <tr><th>Made</th><td>${esc(p.created)}</td></tr>
          <tr><th>Method</th><td>${esc(p.method || '')}</td></tr>
          <tr><th>Emails read</th><td>${(p.excerpts || []).length || p.emails} excerpt(s)</td></tr>
          <tr><th>Documents read</th><td>${files.size ? [...files].map(([f, ls]) => esc(f) + ' <span class="meta">(' + ls.map(esc).join(', ') + ')</span>').join('<br>') : '<span class="none">none</span>'}</td></tr>
          <tr><th>Notion</th><td>Nothing sent: a proposal until approved</td></tr></table></details>`;
      })()}</div>` : '';
  $('detail').innerHTML = `<div class="dealhead"><h2>${esc(d.name)}</h2>
      ${d.url ? `<a class="notion-btn" href="${esc(d.url)}" target="_blank" rel="noopener">Open in Notion &#8599;</a>` : ''}</div>
    <div class="facts">
      <div class="fact"><span class="k">Project ID</span><span class="v ${d.projectId ? 'mono' : 'unset'}">${esc(d.projectId || 'not set in Notion')}</span></div>
      <div class="fact stage ${stageClass(d.status)}"><span class="k">Status</span><span class="v ${d.status ? '' : 'unset'}">${esc(d.status || 'not set')}</span></div>
      <div class="fact"><span class="k">Sector</span><span class="v">${d.team.length ? pills(d.team) : '<span class="unset">not set</span>'}</span></div>
      <div class="fact"><span class="k">Location${d.countries.length > 1 ? 's' : ''}</span><span class="v">${d.countries.length ? pills(d.countries) : '<span class="unset">not set</span>'}</span></div>
    </div>
    ${d.counterparties.length ? `<div class="cps"><span class="k">Counterparties</span>${pills(d.counterparties)}</div>` : ''}
    <p class="meta">Read-only view: nothing has been sent to Notion. Updating Notion will happen only after approval.
      <a class="chatbtn askdeal" data-deal="${esc(d.id)}" data-name="${esc(d.name)}" href="${CHAT}?deal=${encodeURIComponent(d.id)}">Ask about this deal &rarr;</a></p>
    ${dealCard}
    ${notionUpdate(d, p)}
    ${st.state === 'running' ? `<div style="margin-top:8px">${statusHtml(st)}</div>` : ''}
    ${(() => {
      const best = d.threads.filter(t => t.best), alt = d.threads.filter(t => !t.best);
      const open = best.slice(0, DATA.maxOpen || 5), older = best.slice(DATA.maxOpen || 5);
      let h = `<details class="sec"><summary>Email threads (${best.length})</summary>` + (open.map(threadCard).join('') || '<p class="none">No email threads matched.</p>');
      if (older.length) h += `<details style="margin-top:6px"><summary>${older.length} older thread(s)</summary>` +
        older.map(t => `<div class="card"><strong>${esc(t.subject)}</strong> <span class="meta">${esc(t.last)} · ${t.count} email(s)</span><div class="why">Why this deal: ${esc(t.evidence.join('; '))}</div></div>`).join('') + '</details>';
      if (alt.length) h += `<p class="meta">Also possibly related: ${alt.length} thread(s), more likely about ` +
        [...new Map(alt.map(t => [t.topId, t])).values()].map(t => `<a href="#${encodeURIComponent(t.topId)}" onclick="show('${esc(t.topId)}');return false">${esc(t.topName)}</a>`).join(', ') + '.</p>';
      return h + '</details>';
    })()}
    <details class="sec"><summary>Server documents · ${d.docs.filter(x => x.kind === 'about this deal').length} related · ${d.docs.filter(x => x.kind !== 'about this deal').length} mentioned</summary>
    <p class="meta" style="margin:0 0 6px"><b>Related</b>: the file or folder name names the deal or its client (a term sheet, a mission note). <b>Mentioned</b>: the client only appears in the text (reports, newsletters).</p>
    ${(() => {
      const card = x => `<div class="card"><strong>${esc(x.name)}</strong> <span class="meta">${esc(x.date)} · ${x.kind === 'about this deal' ? 'related' : 'mentioned'}${x.also.length ? ' · also as ' + esc(x.also.join(', ')) : ''}</span>
      ${x.link ? ` · <a href="${esc(x.link)}" target="_blank" rel="noopener">open on the server</a>` : ''}
      ${x.path ? `<div class="why" title="Copy into File Explorer if the link does not open">${esc(x.path)}</div>` : ''}
      <div class="why">${esc(x.excerpt)}${x.excerpt.length >= 300 ? '…' : ''}</div></div>`;
      const own = d.docs.filter(x => x.kind === 'about this deal'), other = d.docs.filter(x => x.kind !== 'about this deal');
      if (!own.length) {
        // nothing filed under the deal's name: show the mentions directly, not behind another fold
        return other.length ? `<p class="none">No related document yet. ${other.length} mentioned (reports, newsletters):</p>${other.map(card).join('')}`
                            : '<p class="none">No related or mentioned server document yet.</p>';
      }
      return own.map(card).join('') +
        (other.length ? `<details style="margin-top:6px"><summary>${other.length} mentioned (reports, newsletters)</summary>${other.map(card).join('')}</details>` : '');
    })()}</details>
    <details class="sec"><summary>Contacts (${d.contacts.length})</summary>
    ${d.contacts.map(c => `<div class="meta">${esc(c.name)}${c.job ? ' · ' + esc(c.job) : ''} · ${esc(c.email)}</div>`).join('') || '<p class="none">None in Notion.</p>'}</details>`;
  $('detail').scrollTop = 0;
  history.replaceState(null, '', '#' + encodeURIComponent(id));
}
$('fShow').value = DATA.deals.some(d => d.hasSummary) ? 'summary' : 'mail'; $('q').value = '';
$('chatLink').href = CHAT;
// the menu button folds the side panel (filters and deal list) away; the choice is remembered
try { if (localStorage.getItem('dealsSide') === 'closed') document.body.classList.add('collapsed'); } catch (e) {}
// each section of the side panel remembers whether it is open
['secFilters', 'secDeals', 'secEval', 'secInfo'].forEach(id => {
  try { const v = localStorage.getItem('side_' + id); if (v) $(id).open = v === 'open'; } catch (e) {}
  $(id).addEventListener('toggle', () => { try { localStorage.setItem('side_' + id, $(id).open ? 'open' : 'closed'); } catch (e) {} });
});
// Info: every extract, from the export snapshot (mailboxes by the privacy rule; all sectors)
(function renderInfo() {
  const X = DATA.extracts, live = location.protocol.startsWith('http');
  if (!X) { $('info').innerHTML = '<div class="note">No export snapshot yet: open Evaluation › Email exports and refresh.</div>'; return; }
  const item = (p, html) => live ? `<button data-p="${p}">${html}</button>` : `<div style="padding:6px 14px">${html}</div>`;
  $('info').innerHTML = `<div class="ih">Email exports${X.all ? '' : ' (this mailbox)'}</div>` +
    (X.mail.map(m => item('p6', `<b>${esc(m.name)}</b> <span class="m2">· ${m.error ? 'unreadable' : (m.emails || 0).toLocaleString() + ' emails'}</span>
      <span class="${m.complete ? 'ok2' : 'fl2'}">${m.complete ? '✓ complete' : '· with exceptions'}</span>`)).join('') ||
     '<div class="note">This mailbox\'s own export is not in this folder.</div>') +
    (X.hidden ? `<div class="note">${X.hidden} other mailbox export(s) not shown.</div>` : '') +
    `<div class="ih">Server sectors</div>` +
    (X.sectors.map(s => item('p7', `<b>${esc(s.name)}</b> <span class="m2">· ${(s.docs || 0).toLocaleString()} documents</span>
      <span class="${s.indexed ? 'ok2' : 'fl2'}">${s.indexed ? '· indexed' : '· not indexed here'}</span>`)).join('') || '<div class="note">None found.</div>') +
    `<div class="note">Snapshot of ${esc(X.made)}</div>`;
  $('info').querySelectorAll('button[data-p]').forEach(b => b.onclick = () => showEval(b.dataset.p));
})();
$('menuBtn').onclick = () => { const c = document.body.classList.toggle('collapsed');
  try { localStorage.setItem('dealsSide', c ? 'closed' : 'open'); } catch (e) {} };
// Evaluation shown inside the interface, in the main area (when served by the app)
function showEval(panel) {
  current = '__eval'; $('detail').classList.add('fill');
  $('detail').innerHTML = '<iframe class="evalframe" src="/eval?embed=1' + (panel ? '#' + panel : '') + '" title="Evaluation"></iframe>';
  $('list').querySelectorAll('button').forEach(b => b.setAttribute('aria-current', 'false'));
  $('evalNav').setAttribute('aria-current', 'true');
  history.replaceState(null, '', '#evaluation');
}
// the chat in a panel on this page (when served by the chat program); opened as a file, links go to the chat page
if (location.protocol.startsWith('http')) {
  $('secEval').style.display = '';
  $('evalNav').onclick = () => showEval();
  document.querySelectorAll('#secEval .evp').forEach(b => b.onclick = () => showEval(b.dataset.p));
  const panel = document.createElement('div'); panel.id = 'chatpanel';
  panel.innerHTML = '<div class="bar"><b id="chattitle">Ask about a deal</b><a id="chatfull" href="/" target="_blank">open full page &#8599;</a>' +
                    '<span class="x" id="chatclose" title="Close">&times;</span></div><iframe id="chatframe" title="Chat"></iframe>';
  document.body.appendChild(panel);
  const openChat = (dealId, dealName) => {
    const q = new URLSearchParams({embed: 1}); if (dealId) q.set('deal', dealId);
    const src = '/?' + q.toString();
    if ($('chatframe').getAttribute('src') !== src) $('chatframe').setAttribute('src', src);
    $('chattitle').textContent = dealName ? 'Ask about: ' + dealName : 'Ask about a deal';
    $('chatfull').href = '/' + (dealId ? '?deal=' + encodeURIComponent(dealId) : '');
    panel.classList.add('open');
  };
  $('chatLink').addEventListener('click', e => { e.preventDefault(); openChat('', ''); });
  document.addEventListener('click', e => { const a = e.target.closest('a.askdeal'); if (!a) return;
    e.preventDefault(); openChat(a.dataset.deal, a.dataset.name); });
  $('chatclose').onclick = () => panel.classList.remove('open');
  document.addEventListener('keydown', e => { if (e.key === 'Escape') panel.classList.remove('open'); });
}
const wanted = decodeURIComponent(location.hash.slice(1));
if (wanted && (wanted === '__loose' || DATA.deals.some(d => d.id === wanted))) current = wanted;
['q', 'fShow', 'fSector', 'fProj'].forEach(id => ['input', 'change'].forEach(ev => $(id).addEventListener(ev, () => { current = null; renderList(); })));
renderList();
if (wanted === 'evaluation' && location.protocol.startsWith('http')) showEval();   // a reload keeps the Evaluation view
</script></body></html>"""


if __name__ == "__main__":
    main()