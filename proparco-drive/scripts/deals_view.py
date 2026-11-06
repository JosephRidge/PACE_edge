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


def server_db(cfg):
    published = cfg.get("server_db_path")
    if published and Path(published).exists():
        return Path(published)
    local = cfg["_data_dir"] / "db" / "server.db"
    return local if local.exists() else None


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
    return {"kind": "deal", "latest": r.get("proposed_latest") or "",
            "changed": bool(r.get("description_changed")),
            "history": r.get("history_append") or "", "next": r.get("proposed_next") or "",
            "from": "deal summary", "review": 1 if r.get("review_status") == "needs_review" else 0,
            "emails": src.get("emails", 0), "documents": src.get("documents", []),
            "flags": [f.get("claim", "") if f.get("check") == "notion_outdated"
                      else f"{f.get('check')}: {f.get('claim')}"
                      for f in _json_list(r.get("verification")) if f.get("review")],
            "created": (r.get("created_at") or "")[:16].replace("T", " ")}


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
        pdb.close()
    except sqlite3.Error:
        pass
    any_running = False
    sdb_path = server_db(cfg)
    sdb = read_db(sdb_path) if sdb_path else None

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
        dts = sorted(deal_threads.get(pid, []), key=lambda x: (not x["best"], x["t"]["last"]), reverse=False)
        proposal = next((x for x in dts if x["t"]["result"] and x["best"]), None)
        for x in dts:
            x["status"] = thread_status(x["m"]["conversation_id"], x["t"], note, running)
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
                         "emails": [{"date": e["date"], "from": e["from"], "to": e["to"][:6],
                                     "subject": e["subject"], "body": e["body"][:6000],
                                     "flags": e["flags"], "review": e["review"],
                                     "attachments": e["attachments"]}
                                    for e in ((x["full"] or {}).get("emails") or x["t"]["emails"])]}
                        for x in dts],
            "docs": related_documents(cfg, sdb, names) if (dts or len(order) <= 150) else [],
            "hasEmails": any(x["best"] for x in dts),
        })
    if sdb:
        sdb.close()
    loose = [{"subject": t["subject"], "last": t["last"][:10], "count": t["count"],
              "senders": t["senders"][:3], "unknown": m.get("unknown_addresses", [])[:4]}
             for m, t in sorted(unmatched, key=lambda mt: mt[1]["last"], reverse=True)]

    page = PAGE.replace("__DATA__", json.dumps({"deals": items, "loose": loose}, ensure_ascii=False)
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
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:14px;align-items:center;flex-wrap:wrap}
h1{font-size:19px;margin:0}input[type=search]{padding:7px 10px;border:1px solid var(--line);
border-radius:8px;background:var(--panel);color:var(--ink);min-width:260px}
label{color:var(--muted);font-size:14px}.built{color:var(--muted);font-size:13px;width:100%}
main{display:grid;grid-template-columns:340px 1fr;height:calc(100vh - 92px)}
nav{border-right:1px solid var(--line);overflow:auto}nav button{display:block;width:100%;text-align:left;
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
</style></head><body>
<header><h1>Deals: emails and documents</h1>
<input type="search" id="q" placeholder="Search deals, counterparties, contacts">
<label><input type="checkbox" id="fMail" checked autocomplete="off"> with emails only</label>
<span class="meta" id="count"></span><div class="built">__BUILT__</div></header>
<main><nav id="list"></nav><section id="detail"></section></main>
<script>
const DATA = __DATA__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const para = s => (s && s.trim()) ? esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/\n/g, '<br>') : '<span class="none">(empty)</span>';
let current = null;
function items() {
  const q = $('q').value.toLowerCase().trim();
  const list = DATA.deals.filter(d => !$('fMail').checked || d.hasEmails);
  if (!q) return list;
  return list.filter(d => [d.name, d.status, ...d.counterparties, ...d.contacts.map(c => c.name + ' ' + c.email),
     ...d.threads.map(t => t.subject)].join(' ').toLowerCase().includes(q));
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
  if (current) show(current); else if (shown.length) show(shown[0].id);
}
function statusHtml(st) {
  // Status only: no commands on the page. The command to run is in the tooltip (for the operator).
  const tip = st.cmd ? ` title="To generate: start.bat ${esc(st.cmd)}  (Linux: sh start.sh ${esc(st.cmd)})"` : '';
  if (st.state === 'running') return `<span class="badge run">${esc(st.label)}</span><div class="why">The agent is working on this now; this page updates itself.</div>`;
  if (st.state === 'partial') return `<span class="badge rev"${tip}>Agent summary incomplete</span><div class="why">Generation was interrupted before the end.</div>`;
  if (st.state === 'not_run') return `<span class="badge"${tip}>Agent summary not generated yet</span>`;
  if (st.state === 'none') return '<span class="none">No email threads for this deal yet.</span>';
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
    ${t.status && t.status.state !== 'done' ? `<div style="margin-top:6px">${statusHtml(t.status)}</div>` : ''}
    ${has ? `<details style="margin-top:8px"><summary>Show this thread's agent summary</summary>
      <table style="margin-top:6px"><tr><th>Latest deal</th><td colspan="2">${para(t.agent.latest)}</td></tr>
      <tr><th>History</th><td colspan="2">${para(t.agent.history)}</td></tr>
      <tr><th>Next steps</th><td colspan="2">${para(t.agent.next)}</td></tr></table></details>` : ''}
    ${t.review ? '<span class="badge rev">needs review</span>' : ''}
    ${t.emails.length ? `<details style="margin-top:8px"><summary>Show the ${t.emails.length} email(s) and attachments</summary>
      ${t.emails.map(e => `<div class="card"><strong>${esc(e.date)}</strong> · ${esc(e.subject)}
        <div class="why">From ${esc(e.from)}${e.to.length ? ' to ' + esc(e.to.join(', ')) : ''}</div>
        ${attList(e.attachments)}
        ${e.flags.length ? `<div class="why" style="color:var(--warn)">${e.flags.map(esc).join('<br>')}</div>` : ''}
        <details><summary>Email text</summary><div style="white-space:pre-wrap">${esc(e.body)}</div></details></div>`).join('')}
      </details>` : ''}</div>`;
}
function show(id) {
  current = id;
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
  const holder = !p ? `<td rowspan="3">${statusHtml(st)}</td>` : '';
  const agentCell = key => {
    if (!p) return '';
    if (p.kind === 'deal' && key === 'latest' && !p.changed) return '<span class="none">No change proposed</span>';
    if (p.kind === 'deal' && key === 'history') return p.history ? '<em>To add:</em><br>' + para(p.history) : '<span class="none">No new entries</span>';
    return para(p[key]);
  };
  const row = (label, key, first) => `<tr><th>${label}</th><td>${para(d.notion[key])}</td>${p ? `<td>${agentCell(key)}</td>` : (first ? holder : '')}</tr>`;
  $('detail').innerHTML = `<h2>${esc(d.name)}</h2>
    <p class="meta"><span class="badge">Project ID: ${esc(d.projectId || 'not set in Notion')}</span>
    ${esc(d.status || 'no status')}${d.team.length ? ' · ' + esc(d.team.join(', ')) : ''}${d.countries.length ? ' · ' + esc(d.countries.join(', ')) : ''}
    ${d.url ? ` · <a href="${esc(d.url)}" target="_blank" rel="noopener">Open in Notion</a>` : ''}</p>
    ${d.counterparties.length ? `<p class="meta">Counterparties: ${esc(d.counterparties.join(' · '))}</p>` : ''}
    <h3>On Notion vs agent summary</h3>
    <div class="wrap"><table><tr><th></th><th>On Notion (now)</th><th>Agent summary</th></tr>
    ${row('Latest deal description', 'latest', true)}${row('History of discussions', 'history')}${row('Next steps', 'next')}</table></div>
    ${p && st.state !== 'done' ? `<div style="margin-top:8px">${statusHtml(st)}</div>` : ''}
    ${p && p.kind === 'deal' ? `<p class="meta">Deal summary (${esc(p.created)}) built from: the Notion record · ${p.emails} email evidence line(s)${p.documents.length ? ' · documents: ' + p.documents.map(x => esc(x.file)).join(', ') : ''}</p>`
      : p ? `<p class="meta">Sources: ${esc(p.from)}${d.docs.some(x => x.kind === 'about this deal') ? ' · server documents about this deal' : ''}</p>` : ''}
    ${p && p.kind === 'deal' && p.flags && p.flags.length ? `<p class="meta" style="color:var(--warn)">To check before using: ${p.flags.map(esc).join(' · ')}</p>`
      : p && p.review ? '<p class="meta" style="color:var(--warn)">Some facts in the agent summary could not be verified against its sources: check before using.</p>' : ''}
    <p class="meta">Read-only view: nothing has been sent to Notion. Updating Notion will happen only after approval.</p>
    <h3>Email threads (${d.threads.length})</h3>
    ${d.threads.map(t => threadCard(t)).join('') || '<p class="none">No email threads matched.</p>'}
    <h3>Server documents (${d.docs.length})</h3>
    ${d.docs.map(x => `<div class="card"><strong>${esc(x.name)}</strong> <span class="meta">${esc(x.date)} · ${esc(x.kind)}${x.also.length ? ' · also as ' + esc(x.also.join(', ')) : ''}</span>
      ${x.link ? ` · <a href="${esc(x.link)}" target="_blank" rel="noopener">open on the server</a>` : ''}
      ${x.path ? `<div class="why" title="Copy into File Explorer if the link does not open">${esc(x.path)}</div>` : ''}
      <div class="why">${esc(x.excerpt)}${x.excerpt.length >= 300 ? '…' : ''}</div></div>`).join('') || '<p class="none">No indexed server documents mention this deal yet.</p>'}
    <h3>Contacts (${d.contacts.length})</h3>
    ${d.contacts.map(c => `<div class="meta">${esc(c.name)}${c.job ? ' · ' + esc(c.job) : ''} · ${esc(c.email)}</div>`).join('') || '<p class="none">None in Notion.</p>'}`;
  $('detail').scrollTop = 0;
  history.replaceState(null, '', '#' + encodeURIComponent(id));
}
$('fMail').checked = true; $('q').value = '';
const wanted = decodeURIComponent(location.hash.slice(1));
if (wanted && (wanted === '__loose' || DATA.deals.some(d => d.id === wanted))) current = wanted;
['q', 'fMail'].forEach(id => $(id).addEventListener('input', () => { current = null; renderList(); }));
renderList();
</script></body></html>"""


if __name__ == "__main__":
    main()