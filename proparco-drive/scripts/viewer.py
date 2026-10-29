#!/usr/bin/env python3
"""
viewer.py - build an offline page to browse the database: one entry per email thread with
its title, senders and recipients, Latest Deal Summary, Next Steps, Project History, the
emails themselves and links to their attachments.

    python3 scripts/viewer.py            # build <data_dir>/viewer.html and open it
    python3 scripts/viewer.py --no-open  # build only

The page is written into the data folder because it contains email content: it stays on
the laptop, never on the flash disk. Attachment links point to the files in
<data_dir>/acquisitions/<run>/attachments/, relative to the page, so they keep working if
the whole data folder is moved. Rebuild the page to see newer results.
"""
import argparse
import json
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed as E  # noqa: E402  (acquisition reading helpers)
from engine import load_config  # noqa: E402
from generate import identify  # noqa: E402

PREFIX = re.compile(r"^\s*((re|aw|r|rif|sv|fw|fwd|tr|wg|rv|i|forward)\s*:\s*)+", re.I)


def clean_title(subject):
    return PREFIX.sub("", subject or "").strip() or "(no subject)"


def attachment_index(data_dir, run_folders):
    """(run folder, message key) -> attachment records with a link relative to the page."""
    index = {}
    for run in run_folders:
        run_dir = data_dir / "acquisitions" / run
        path = run_dir / "acquisition.json"
        if not path.exists():
            continue
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        for m in E.as_list(data.get("messages")):
            key = m.get("entryId") or ""
            records = []
            for a in E.as_list(m.get("attachments")):
                if not isinstance(a, dict):
                    continue
                href = None
                local = a.get("localPath") or ""
                candidates = [re.split(r"[\\/]", local)[-1]] if local else []
                candidates.append(a.get("filename") or "")
                for name in candidates:
                    if name and (run_dir / "attachments" / name).exists():
                        href = "/".join(quote(p) for p in ("acquisitions", run, "attachments", name))
                        break
                records.append({"name": a.get("filename") or "attachment",
                                "sizeMB": a.get("sizeMB"), "href": href,
                                "reason": None if href else (a.get("skipReason")
                                                             or "file not found in the data folder")})
            index[(run, key)] = records
            fp = E.hashlib.sha1(f"{m.get('sender') or ''}|{m.get('receivedTime') or ''}|"
                                f"{m.get('subject')}".encode("utf-8")).hexdigest()
            index[(run, fp)] = records
    return index


def collect(cfg):
    path = cfg["_data_dir"] / "db" / "proparco.db"
    if not path.exists():
        sys.exit(f"No database at {path}. Run embed.py first.")
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    internal = [d.lower() for d in cfg.get("internal_domains", [])]

    msgs = db.execute("SELECT * FROM messages ORDER BY COALESCE(received_iso, received_raw)").fetchall()
    results = {}
    if "email_results" in tables:
        results = {r["entry_id"]: r for r in db.execute("SELECT * FROM email_results")}
    states = {}
    if "thread_state" in tables:
        states = {r["conversation_id"]: r for r in db.execute("SELECT * FROM thread_state")}
    attachments = attachment_index(cfg["_data_dir"], {m["run_folder"] for m in msgs})

    threads = {}
    for m in msgs:
        threads.setdefault(m["conversation_id"], []).append(m)

    out = []
    for conv, items in threads.items():
        people = {}
        emails = []
        for m in items:
            key, label, inside = identify(m["sender"], internal)
            p = people.setdefault(key, {"label": label, "internal": inside, "sent": 0, "received": 0})
            p["sent"] += 1
            recipients = json.loads(m["recipients"] or "[]")
            for r in recipients:
                k, lab, ins = identify(r, internal)
                q = people.setdefault(k, {"label": lab, "internal": ins, "sent": 0, "received": 0})
                q["received"] += 1
            res = results.get(m["entry_id"])
            flags = json.loads(res["verification"] or "[]") if res is not None and \
                "verification" in res.keys() else []
            emails.append({
                "date": (m["received_iso"] or m["received_raw"] or "")[:16].replace("T", " "),
                "from": identify(m["sender"], internal)[1],
                "to": [identify(r, internal)[1] for r in recipients],
                "subject": m["subject"] or "",
                "body": m["body_clean"] or "",
                "review": res["review_status"] if res is not None else None,
                "flags": [f"{f['field']}: {f['check']} → {f['claim']}" for f in flags
                          if f.get("review")],
                "attachments": attachments.get((m["run_folder"], m["entry_id"]), []),
            })
        last = None
        for m in reversed(items):
            if m["entry_id"] in results:
                last = results[m["entry_id"]]
                break
        state = states.get(conv)
        out.append({
            "id": conv,
            "title": clean_title(items[0]["subject"]),
            "first": (items[0]["received_iso"] or "")[:10],
            "last": (items[-1]["received_iso"] or "")[:10],
            "count": len(items),
            "generated": sum(1 for m in items if m["entry_id"] in results),
            "deal": bool(state["deal_related"]) if state is not None else None,
            "needsReview": sum(1 for e in emails if e["review"] == "needs_review"),
            "attachments": sum(len(e["attachments"]) for e in emails),
            "people": sorted(people.values(), key=lambda p: (-p["sent"], -p["received"])),
            "summary": last["latest_deal_summary"] if last is not None else None,
            "steps": last["next_steps"] if last is not None else None,
            "history": last["project_history"] if last is not None else None,
            "emails": emails,
        })
    out.sort(key=lambda t: t["last"], reverse=True)
    db.close()
    return out


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Email threads</title>
<style>
:root { color-scheme: light; --bg:#f4f7f8; --surface:#ffffff; --ink:#123b49; --text:#1d2a30;
  --muted:#5e6d74; --rule:#d5dee1; --pick:#e3eef2; --ok:#2f7d4f; --warn:#a86b12; --bad:#a3243b; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --bg:#0f171a; --surface:#152126;
  --ink:#9fd3e0; --text:#dde8eb; --muted:#93a4ab; --rule:#2a3a40; --pick:#1d3038;
  --ok:#6cc08e; --warn:#e0a948; --bad:#ef7d8f; } }
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; }
body { background: var(--bg); color: var(--text);
  font: 15px/1.5 Seravek, "Gill Sans Nova", Ubuntu, Calibri, "DejaVu Sans", sans-serif;
  display: grid; grid-template-rows: auto 1fr; }
header { padding: 14px 20px; border-bottom: 1px solid var(--rule); display: flex; gap: 16px;
  align-items: center; flex-wrap: wrap; }
header h1 { font-size: 18px; margin: 0; color: var(--ink); font-weight: 600; }
header .note { color: var(--muted); font-size: 13px; }
input[type=search] { font: inherit; padding: 6px 10px; border: 1px solid var(--rule);
  border-radius: 6px; background: var(--surface); color: var(--text); min-width: 240px; }
label { font-size: 13px; color: var(--muted); display: inline-flex; gap: 4px; align-items: center; }
.layout { display: grid; grid-template-columns: minmax(260px, 360px) 1fr; min-height: 0; }
nav { border-right: 1px solid var(--rule); overflow-y: auto; }
nav button { all: unset; display: block; width: 100%; padding: 10px 16px;
  border-bottom: 1px solid var(--rule); cursor: pointer; box-sizing: border-box; }
nav button:hover, nav button:focus-visible { background: var(--pick); }
nav button[aria-current=true] { background: var(--pick); box-shadow: inset 3px 0 var(--ink); }
nav .t { font-weight: 600; color: var(--text); }
nav .m { font-size: 13px; color: var(--muted); }
main { overflow-y: auto; padding: 20px 28px 48px; }
main h2 { font-size: 22px; color: var(--ink); margin: 0 0 4px; line-height: 1.25; }
main h3 { font-size: 15px; color: var(--ink); margin: 26px 0 8px; }
.meta { color: var(--muted); font-size: 14px; }
.field { white-space: pre-wrap; background: var(--surface); border: 1px solid var(--rule);
  border-radius: 8px; padding: 12px 14px; max-width: 80ch; }
.empty { color: var(--muted); font-style: italic; }
.people { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: 14px; }
.people .ext { color: var(--text); } .people .int { color: var(--muted); }
details.email { border: 1px solid var(--rule); border-radius: 8px; background: var(--surface);
  margin: 8px 0; max-width: 90ch; }
details.email summary { cursor: pointer; padding: 10px 14px; list-style: none; }
details.email summary::-webkit-details-marker { display: none; }
details.email .body { padding: 0 14px 12px; }
details.email pre { white-space: pre-wrap; font: inherit; font-size: 14px; margin: 8px 0 0;
  color: var(--text); }
.badge { font-size: 12px; padding: 1px 7px; border-radius: 10px; border: 1px solid currentColor;
  margin-left: 6px; white-space: nowrap; }
.ok { color: var(--ok); } .warn { color: var(--warn); } .bad { color: var(--bad); }
ul.att { margin: 6px 0 0; padding-left: 18px; font-size: 14px; }
a { color: var(--ink); }
@media (max-width: 760px) { .layout { grid-template-columns: 1fr; }
  nav { max-height: 40vh; border-right: 0; border-bottom: 1px solid var(--rule); } }
</style></head><body>
<header><h1>Email threads</h1>
<input type="search" id="q" placeholder="Search titles, people, summaries" aria-label="Search">
<label><input type="checkbox" id="fGen"> generated</label>
<label><input type="checkbox" id="fDeal"> deal related</label>
<label><input type="checkbox" id="fRev"> needs review</label>
<label><input type="checkbox" id="fAtt"> with attachments</label>
<span class="note" id="count"></span>
<span class="note">Built __BUILT__ · local only, rebuild for new results</span></header>
<div class="layout"><nav id="list" aria-label="Threads"></nav><main id="detail"></main></div>
<script id="data" type="application/json">__DATA__</script>
<script>
const threads = JSON.parse(document.getElementById('data').textContent);
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let current = null;

function matches(t) {
  const q = $('q').value.trim().toLowerCase();
  if ($('fGen').checked && !t.generated) return false;
  if ($('fDeal').checked && !t.deal) return false;
  if ($('fRev').checked && !t.needsReview) return false;
  if ($('fAtt').checked && !t.attachments) return false;
  if (!q) return true;
  const hay = [t.title, t.summary, t.steps, t.history, ...t.people.map(p => p.label),
               ...t.emails.map(e => e.subject)].join(' ').toLowerCase();
  return q.split(/\\s+/).every(w => hay.includes(w));
}

function renderList() {
  const shown = threads.filter(matches);
  $('count').textContent = `${shown.length} of ${threads.length} threads`;
  $('list').innerHTML = shown.map(t => `<button data-id="${esc(t.id)}" aria-current="${t.id === current}">
    <div class="t">${esc(t.title)}</div>
    <div class="m">${esc(t.last)} · ${t.count} email${t.count > 1 ? 's' : ''}${t.attachments ? ' · ' + t.attachments + ' attachment' + (t.attachments > 1 ? 's' : '') : ''}
    ${t.needsReview ? '<span class="badge warn">review</span>' : ''}${t.deal ? '<span class="badge ok">deal</span>' : ''}</div></button>`).join('');
  $('list').querySelectorAll('button').forEach(b => b.onclick = () => show(b.dataset.id));
  if (!current && shown.length) show(shown[0].id);
}

function field(text, none) {
  return text ? `<div class="field">${esc(text).replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>')}</div>`
              : `<p class="empty">${none}</p>`;
}

function show(id) {
  current = id;
  const t = threads.find(x => x.id === id);
  $('list').querySelectorAll('button').forEach(b => b.setAttribute('aria-current', b.dataset.id === id));
  const people = t.people.map(p => `<span class="${p.internal ? 'int' : 'ext'}">${esc(p.label)}
      <span class="meta">(${p.sent} sent, ${p.received} received${p.internal ? ', internal' : ''})</span></span>`).join('');
  const notGen = t.generated ? '' : '<p class="empty">Fields not generated yet for this thread (menu: Generate summaries for ONE thread).</p>';
  const emails = t.emails.map(e => `<details class="email"><summary>
      <strong>${esc(e.date)}</strong> · ${esc(e.from)}
      ${e.review === 'needs_review' ? '<span class="badge warn">needs review</span>' : e.review ? `<span class="badge">${esc(e.review)}</span>` : ''}
      ${e.attachments.length ? `<span class="badge">${e.attachments.length} attachment${e.attachments.length > 1 ? 's' : ''}</span>` : ''}
      <div class="meta">To: ${esc(e.to.join(', ') || 'unknown')}</div>
      <div class="meta">${esc(e.subject)}</div></summary>
      <div class="body">
      ${e.flags.length ? `<p class="warn">Could not verify: ${e.flags.map(esc).join('; ')}</p>` : ''}
      ${e.attachments.length ? '<ul class="att">' + e.attachments.map(a => a.href
          ? `<li><a href="${a.href}" target="_blank" rel="noopener">${esc(a.name)}</a>${a.sizeMB ? ` <span class="meta">${a.sizeMB} MB</span>` : ''}</li>`
          : `<li>${esc(a.name)} <span class="meta">(not available: ${esc(a.reason)})</span></li>`).join('') + '</ul>' : ''}
      <pre>${esc(e.body) || '<span class="empty">No text after cleaning.</span>'}</pre></div></details>`).join('');
  $('detail').innerHTML = `<h2>${esc(t.title)}</h2>
    <p class="meta">${esc(t.first)} to ${esc(t.last)} · ${t.count} emails · ${t.generated} with generated fields
      ${t.needsReview ? ` · <span class="warn">${t.needsReview} need review</span>` : ''}</p>
    ${notGen}
    <h3>Senders and recipients</h3><div class="people">${people || '<span class="empty">Unknown</span>'}</div>
    <h3>Latest Deal Summary</h3>${field(t.summary, 'Not generated yet.')}
    <h3>Next Steps</h3>${field(t.steps, 'Not generated yet.')}
    <h3>Project History</h3>${field(t.history, 'Not generated yet.')}
    <h3>Emails and attachments</h3>${emails}`;
  $('detail').scrollTop = 0;
  history.replaceState(null, '', '#' + encodeURIComponent(id));
}

['q', 'fGen', 'fDeal', 'fRev', 'fAtt'].forEach(i => $(i).addEventListener('input', renderList));
const fromHash = decodeURIComponent(location.hash.slice(1));
if (fromHash && threads.some(t => t.id === fromHash)) current = fromHash;
renderList();
if (current) show(current);
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Build an offline page to browse the email threads.")
    ap.add_argument("--no-open", action="store_true", help="build the page without opening it")
    args = ap.parse_args()
    cfg = load_config()
    threads = collect(cfg)
    data = json.dumps(threads, ensure_ascii=False).replace("</", "<\\/")
    page = PAGE.replace("__DATA__", data).replace("__BUILT__", datetime.now().strftime("%d/%m/%Y %H:%M"))
    out = cfg["_data_dir"] / "viewer.html"
    out.write_text(page, encoding="utf-8")
    generated = sum(1 for t in threads if t["generated"])
    print(f"Viewer written: {out}")
    print(f"{len(threads)} threads, {generated} with generated fields, "
          f"{sum(t['attachments'] for t in threads)} attachments linked or listed.")
    if not args.no_open:
        webbrowser.open(out.as_uri())


if __name__ == "__main__":
    main()