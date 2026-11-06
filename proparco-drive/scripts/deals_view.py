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
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import load_config  # noqa: E402

INACTIVE = ("closed", "repaid", "exited", "written-off", "dropped", "abandoned")
DOCS_PER_DEAL = 8


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
    for m in db.execute("""SELECT entry_id, conversation_id, subject, sender, received_iso
                           FROM messages ORDER BY received_iso"""):
        conv = m["conversation_id"] or ""
        if conv.startswith("folder:"):
            continue
        t = threads.setdefault(conv, {"subject": m["subject"] or "", "count": 0, "last": "",
                                      "senders": [], "result": None, "review": 0})
        t["count"] += 1
        t["last"] = max(t["last"], m["received_iso"] or "")
        if m["sender"] and m["sender"] not in t["senders"]:
            t["senders"].append(m["sender"])
        res = results.get(m["entry_id"])
        if res:
            if res.get("review_status") == "needs_review":
                t["review"] += 1
            if any(res.get(k) for k in ("latest_deal_summary", "project_history", "next_steps")):
                t["result"] = res                       # the latest email with fields wins
    db.close()
    return threads


def server_db(cfg):
    published = cfg.get("server_db_path")
    if published and Path(published).exists():
        return Path(published)
    local = cfg["_data_dir"] / "db" / "server.db"
    return local if local.exists() else None


def doc_link(cfg, body):
    first = (body or "").split("\n", 1)[0]
    if not first.startswith("Document: "):
        return None
    parts = [x for x in re.split(r"[\\/]", first[len("Document: "):]) if x]
    base = Path(cfg.get("sdrive_link_base", "S:\\"))
    try:
        return base.joinpath(*parts).as_uri() if parts and base.is_absolute() else None
    except ValueError:
        return None


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
        item = {"name": name, "date": (r["received_iso"] or "")[:10], "also": [],
                "excerpt": " ".join(text.split())[:300], "link": doc_link(cfg, body),
                "kind": "about this deal" if named else ("newsletter mention" if news else "mentions it"),
                "rank": 0 if named else (2 if news else 1)}
        seen[key] = item
        found.append(item)
    found.sort(key=lambda x: (x["rank"], -int(x["date"].replace("-", "") or 0)))
    return found[:DOCS_PER_DEAL]


def core_name(deal_name):
    """'Family Bank - Equity 2026' -> 'Family Bank' (the part naming the company)."""
    return re.split(r"\s+[-–]\s+", deal_name or "", maxsplit=1)[0].strip()


def main():
    ap = argparse.ArgumentParser(description="Build the deal-centred page.")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser")
    ap.add_argument("--all", action="store_true",
                    help="list every deal (default: deals with emails, then live deals)")
    args = ap.parse_args()

    cfg = load_config()
    ndir = cfg["_data_dir"] / "notion"
    if not (ndir / "directory.json").exists():
        sys.exit("No Notion directory yet. Run notion_sync.py first.")
    d = json.loads((ndir / "directory.json").read_text(encoding="utf-8"))
    matches = json.loads((ndir / "matches.json").read_text(encoding="utf-8")) \
        if (ndir / "matches.json").exists() else []
    if not matches:
        print("Note: no matches.json yet (run notion_match.py): deals will show no emails.")

    projects = {p["id"]: p for p in d["projects"]}
    cps = {c["id"]: c for c in d["counterparties"]}
    contacts_of_cp = defaultdict(list)
    for c in d["contacts"]:
        for cid in c["organisations"]:
            contacts_of_cp[cid].append(c)

    threads = email_threads(cfg["_data_dir"] / "db" / "proparco.db")
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
                    "m": m, "t": t, "score": c["score"], "evidence": c["evidence"],
                    "best": rank == 0 or c["score"] >= top})

    sdb_path = server_db(cfg)
    sdb = read_db(sdb_path) if sdb_path else None

    with_emails = [pid for pid in projects if any(x["best"] for x in deal_threads.get(pid, []))]
    live = [pid for pid, p in projects.items()
            if pid not in with_emails and not any(w in (p["status"] or "").lower() for w in INACTIVE)]
    rest = [pid for pid in projects if pid not in with_emails and pid not in live]
    order = sorted(with_emails, key=lambda pid: max(x["t"]["last"] for x in deal_threads[pid] if x["best"]),
                   reverse=True) + sorted(live, key=lambda pid: projects[pid]["edited"], reverse=True)
    if args.all:
        order += sorted(rest, key=lambda pid: projects[pid]["name"].lower())

    items = []
    for pid in order:
        p = projects[pid]
        names = sorted({core_name(p["name"])} | {cps[c]["name"] for c in p["counterparties"] if c in cps})
        dts = sorted(deal_threads.get(pid, []), key=lambda x: (not x["best"], x["t"]["last"]), reverse=False)
        proposal = next((x for x in dts if x["t"]["result"] and x["best"]), None)
        items.append({
            "id": pid, "name": p["name"], "status": p["status"], "team": p["team"],
            "countries": p["countries"], "url": p["url"], "projectId": p["project_id"],
            "counterparties": [cps[c]["name"] for c in p["counterparties"] if c in cps],
            "contacts": [{"name": c["name"], "email": c["email"], "job": c["job"]}
                         for cid in p["counterparties"] for c in contacts_of_cp.get(cid, [])][:8],
            "notion": {"latest": p.get("latest", ""), "history": p.get("history", ""),
                       "next": p.get("next", "")},
            "proposal": None if proposal is None else {
                "latest": proposal["t"]["result"].get("latest_deal_summary") or "",
                "history": proposal["t"]["result"].get("project_history") or "",
                "next": proposal["t"]["result"].get("next_steps") or "",
                "from": proposal["t"]["subject"], "review": proposal["t"]["review"]},
            "threads": [{"subject": x["t"]["subject"], "last": x["t"]["last"][:10],
                         "count": x["t"]["count"], "senders": x["t"]["senders"][:4],
                         "score": x["score"], "evidence": x["evidence"], "best": x["best"],
                         "summary": (x["t"]["result"] or {}).get("latest_deal_summary") or "",
                         "review": x["t"]["review"]} for x in dts],
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
    out = cfg["_data_dir"] / "deals.html"
    out.write_text(page, encoding="utf-8")
    print(f"Deal view written: {out}")
    print(f"   {len(with_emails)} deals with matched emails, {len(live)} other live deals"
          f"{'' if not args.all else f', {len(rest)} closed or dropped'}; "
          f"{len(loose)} email threads without a deal")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())


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
.mail{color:var(--good);border-color:var(--good)}.rev{color:var(--warn);border-color:var(--warn)}
section{overflow:auto;padding:18px 26px}h2{margin:0 0 4px;font-size:21px}h3{margin:22px 0 8px;font-size:16px}
.meta{color:var(--muted);font-size:14px}a{color:var(--accent)}
table{border-collapse:collapse;width:100%;background:var(--panel);border:1px solid var(--line);border-radius:8px}
td,th{border-bottom:1px solid var(--line);padding:9px 11px;vertical-align:top;text-align:left}
th{font-size:13px;color:var(--muted);font-weight:600;width:16%}td{width:42%}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 13px;margin:8px 0}
.none{color:var(--muted);font-style:italic}.why{font-size:13px;color:var(--muted)}
.wrap{overflow-x:auto}
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
const para = s => (s && s.trim()) ? esc(s).replace(/\n/g, '<br>') : '<span class="none">(empty)</span>';
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
    ${d.threads.some(t => t.review) ? '<span class="badge rev">needs review</span>' : ''}</div></button>`).join('')
    + (DATA.loose.length ? `<button data-id="__loose" aria-current="${current === '__loose'}"><div class="n">Emails without a deal</div>
       <div class="m">${DATA.loose.length} thread(s): attach later</div></button>` : '');
  $('list').querySelectorAll('button').forEach(b => b.onclick = () => show(b.dataset.id));
  if (!current && shown.length) show(shown[0].id);
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
  const d = DATA.deals.find(x => x.id === id), p = d.proposal;
  const row = (label, key) => `<tr><th>${label}</th><td>${para(d.notion[key])}</td><td>${p ? para(p[key]) : '<span class="none">no proposal yet</span>'}</td></tr>`;
  $('detail').innerHTML = `<h2>${esc(d.name)}</h2>
    <p class="meta">${esc(d.status || 'no status')}${d.team.length ? ' · ' + esc(d.team.join(', ')) : ''}${d.countries.length ? ' · ' + esc(d.countries.join(', ')) : ''}${d.projectId ? ' · ' + esc(d.projectId) : ''}
    ${d.url ? ` · <a href="${esc(d.url)}" target="_blank" rel="noopener">Open in Notion</a>` : ''}</p>
    ${d.counterparties.length ? `<p class="meta">Counterparties: ${esc(d.counterparties.join(' · '))}</p>` : ''}
    <h3>Notion now, and the assistant's proposal</h3>
    <div class="wrap"><table><tr><th></th><th>In Notion now</th><th>Proposed from the emails${p ? ` (from “${esc(p.from)}”)` : ''}</th></tr>
    ${row('Latest deal description', 'latest')}${row('History of discussions', 'history')}${row('Next steps', 'next')}</table></div>
    ${p && p.review ? '<p class="meta" style="color:var(--warn)">Some facts in the proposal could not be verified against the emails: check before using.</p>' : ''}
    <p class="meta">Read-only view: nothing has been sent to Notion. Updating Notion will happen only after approval.</p>
    <h3>Email threads (${d.threads.length})</h3>
    ${d.threads.map(t => `<div class="card"><strong>${esc(t.subject)}</strong> <span class="meta">${esc(t.last)} · ${t.count} email(s) · match ${t.score.toFixed(1)}${t.best ? '' : ' (alternative)'}</span>
      <div class="why">Why: ${esc(t.evidence.join('; '))}</div>
      ${t.summary ? `<p>${para(t.summary)}</p>` : '<p class="none">Not summarised yet.</p>'}
      ${t.review ? '<span class="badge rev">needs review</span>' : ''}</div>`).join('') || '<p class="none">No email threads matched.</p>'}
    <h3>Server documents (${d.docs.length})</h3>
    ${d.docs.map(x => `<div class="card"><strong>${esc(x.name)}</strong> <span class="meta">${esc(x.date)} · ${esc(x.kind)}${x.also.length ? ' · also as ' + esc(x.also.join(', ')) : ''}</span>
      ${x.link ? ` · <a href="${esc(x.link)}" target="_blank" rel="noopener">open on the server</a>` : ''}
      <div class="why">${esc(x.excerpt)}${x.excerpt.length >= 300 ? '…' : ''}</div></div>`).join('') || '<p class="none">No indexed server documents mention this deal yet.</p>'}
    <h3>Contacts (${d.contacts.length})</h3>
    ${d.contacts.map(c => `<div class="meta">${esc(c.name)}${c.job ? ' · ' + esc(c.job) : ''} · ${esc(c.email)}</div>`).join('') || '<p class="none">None in Notion.</p>'}`;
  $('detail').scrollTop = 0;
}
$('fMail').checked = true; $('q').value = '';
['q', 'fMail'].forEach(id => $(id).addEventListener('input', () => { current = null; renderList(); }));
renderList();
</script></body></html>"""


if __name__ == "__main__":
    main()