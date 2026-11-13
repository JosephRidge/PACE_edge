#!/usr/bin/env python3
"""
eval_view.py - the Evaluation page: how well the assistant performs, from what is already saved.

Served by chat.py at /eval (beside /deals and the chat). READ-ONLY: it only reads the saved
results and changes nothing. Panels:
  1. Search quality   the latest search test report (eval_search.py), else the recorded result
  2. Summaries        deals summarised, ready or held back, what was added and what was refused
  3. Approvals        approved, approved after checking, still waiting
  4. Chat answers     answers given, time per answer, answers with a warning
  5. Coverage         emails and server documents indexed, Notion copy, threads matched to deals
  6. Email exports    each mailbox export: exported vs in Outlook, not read, period, verdict, main finding
  7. Server extraction each sector: files listed, documents extracted, text, skipped by reason, indexed
Panels 6 and 7 read every exported file (minutes for the large sectors), so they show a saved
SNAPSHOT with its date; "Refresh" recomputes it in the background. Command line: eval_view.py --snapshot
"""
import glob
import html
import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def esc(x):
    return html.escape(str(x if x is not None else ""))


def ro(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def has_table(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone())


# ---------------------------------------------------------------- 1. search quality

REPORT_LINE = re.compile(r"found first (\d+)% \| in top 5 (\d+)% \| in top (\d+) (\d+)% \| MRR ([\d.]+)")


def search_quality(cfg):
    """The newest eval-*.md report saved by eval_search.py, else the recorded result file."""
    places = [cfg["_data_dir"], cfg["_data_dir"] / "eval", HERE, HERE.parent, HERE / "eval",
              Path(os.path.expanduser("~/proparco-work"))]
    reports = sorted({p for d in places for p in glob.glob(str(d / "eval-*.md"))}, key=os.path.getmtime, reverse=True)
    for rp in reports:
        text = Path(rp).read_text(encoding="utf-8", errors="replace")
        m = REPORT_LINE.search(text)
        if not m:
            continue
        n = re.search(r"\*\*(\d+) questions with a known answer", text)
        sp = re.search(r"search median ([\d.]+)s", text)
        misses = re.findall(r"^\*\*(Q\d+)\*\* ", text, re.M)
        title = re.search(r"^# Search evaluation: (.+)$", text, re.M)
        return {"source": f"report {Path(rp).name}", "when": datetime.fromtimestamp(os.path.getmtime(rp)).strftime("%d/%m/%Y"),
                "db": title.group(1) if title else "", "n": int(n.group(1)) if n else None,
                "hit1": int(m.group(1)), "hit5": int(m.group(2)), "k": int(m.group(3)), "hitk": int(m.group(4)),
                "mrr": float(m.group(5)), "search_s": float(sp.group(1)) if sp else None, "misses": misses}
    rec = HERE / "eval" / "results_recorded.json"
    if rec.exists():
        r = json.loads(rec.read_text(encoding="utf-8"))
        r["source"] = f"recorded result ({r.get('note', 'see file')})"
        return r
    return None


# ---------------------------------------------------------------- 2 and 3. summaries and approvals

def summaries(cfg):
    path = cfg["_data_dir"] / "db" / "proparco.db"
    if not path.exists():
        return None
    db = ro(path)
    try:
        if not has_table(db, "deal_proposals"):
            return None
        rows = [dict(r) for r in db.execute("SELECT * FROM deal_proposals")]
        appr = {}
        if has_table(db, "deal_approvals"):
            cols = {c[1] for c in db.execute("PRAGMA table_info(deal_approvals)")}
            for a in db.execute("SELECT * FROM deal_approvals"):
                a = dict(a)
                a["after_checking"] = a.get("after_checking", 0) if "after_checking" in cols else 0
                appr[a["project_id"]] = a
    finally:
        db.close()
    out = {"total": len(rows), "ready": 0, "flagged": 0, "changed": 0, "history": 0, "steps": 0, "done": 0,
           "refused": 0, "retrieval": 0, "approved": 0, "approved_checked": 0, "waiting": 0, "deals": []}
    for r in rows:
        src = {}
        try:
            src = json.loads(r.get("sources_json") or "{}")
        except ValueError:
            pass
        flagged = r.get("review_status") == "needs_review"
        h, st, dn = int(src.get("history_added") or 0), int(src.get("steps_added") or 0), int(src.get("steps_done") or 0)
        refused = len(src.get("not_added") or [])
        changed = bool(r.get("description_changed") or h or st or dn or (r.get("history_append") or "").strip())
        a = appr.get(r["project_id"])
        approved = bool(a and a.get("proposal_created") == r.get("created_at"))
        out["flagged" if flagged else "ready"] += 1
        out["changed"] += changed
        out["history"] += h
        out["steps"] += st
        out["done"] += dn
        out["refused"] += refused
        out["retrieval"] += (r.get("prompt_version") or "").endswith("retrieve")
        if approved:
            out["approved"] += 1
            out["approved_checked"] += bool(a.get("after_checking"))
        elif changed:
            out["waiting"] += 1
        out["deals"].append({"name": r.get("deal_name") or r["project_id"], "flagged": flagged, "changed": changed,
                             "fact": bool(r.get("description_changed")), "history": h, "steps": st, "done": dn,
                             "refused": refused, "approved": approved,
                             "checked": bool(a and a.get("after_checking")) if approved else False,
                             "made": (r.get("created_at") or "")[:16].replace("T", " ")})
    out["deals"].sort(key=lambda d: (not d["changed"], d["name"].lower()))
    return out


# ---------------------------------------------------------------- 4. chat answers

def chat_answers(cfg):
    folder = cfg["_data_dir"] / "chat"
    turns = []
    for f in sorted(folder.glob("history*.jsonl")) if folder.is_dir() else []:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                turns.append(json.loads(line))
            except ValueError:
                continue
    if not turns:
        return None
    secs = sorted(t.get("seconds") or 0 for t in turns)
    return {"answers": len(turns), "deals": len({t.get("deal") for t in turns if t.get("deal")}),
            "general": sum(1 for t in turns if not t.get("deal")),
            "median_s": secs[len(secs) // 2], "warned": sum(1 for t in turns if t.get("flags")),
            "sources": round(sum(len(t.get("sources") or []) for t in turns) / len(turns), 1)}


# ---------------------------------------------------------------- 5. coverage

def coverage(cfg):
    from sources import server_databases
    out = {"emails": None, "sectors": [], "notion": None, "matched": None}
    p = cfg["_data_dir"] / "db" / "proparco.db"
    if p.exists():
        db = ro(p)
        try:
            out["emails"] = (db.execute("SELECT count(*) FROM messages").fetchone()[0],
                             db.execute("SELECT count(*) FROM chunks").fetchone()[0])
        finally:
            db.close()
    for label, path in server_databases(cfg):
        try:
            db = ro(path)
            out["sectors"].append((label, db.execute("SELECT count(*) FROM messages").fetchone()[0],
                                   db.execute("SELECT count(*) FROM chunks").fetchone()[0]))
            db.close()
        except Exception:
            out["sectors"].append((label, None, None))
    nd = cfg["_data_dir"] / "notion"
    try:
        d = json.loads((nd / "directory.json").read_text(encoding="utf-8"))
        out["notion"] = (len(d.get("projects", [])),
                         datetime.fromtimestamp((nd / "directory.json").stat().st_mtime).strftime("%d/%m/%Y"))
        m = json.loads((nd / "matches.json").read_text(encoding="utf-8"))
        out["matched"] = (sum(1 for x in m if x.get("candidates")), len(m))
    except (OSError, ValueError):
        pass
    return out


# ---------------------------------------------------------------- 6. director's test set

def director_set():
    f = HERE / "eval" / "questions_director.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        qs = d.get("questions", d) if isinstance(d, dict) else d
        return {"count": len(qs), "file": f.name}
    except ValueError:
        return {"count": 0, "file": f.name}


# ---------------------------------------------------------------- 6 and 7. export snapshot (mail + server)

def snapshot_path(cfg):
    return cfg["_data_dir"] / "eval" / "exports-snapshot.json"


def default_roots():
    """Where exports live: this laptop's work and backup folders, and every mounted flash disk."""
    env = os.environ.get("PROPARCO_EVAL_ROOTS")
    if env:
        return [r for r in env.split(os.pathsep) if r]
    home = os.path.expanduser("~")
    return [os.path.join(home, "proparco-work"), os.path.join(home, "proparco-backup")] + glob.glob("/media/*/*") + glob.glob("/media/*")


def sector_folders(roots):
    """Folders holding a sector extraction (status.json, acquisitions/ and logs/), at most 3 levels down
    (never descending into the extracted files themselves)."""
    seen, out = set(), []
    for r in roots:
        for pat in ("status.json", "*/status.json", "*/*/status.json", "*/*/*/status.json"):
            for st in glob.glob(os.path.join(r, pat)):
                f = os.path.dirname(st)
                real = os.path.realpath(f)
                if real in seen or not (os.path.isdir(os.path.join(f, "acquisitions")) and os.path.isdir(os.path.join(f, "logs"))):
                    continue
                seen.add(real)
                out.append(f)
    return out


def take_snapshot(cfg, roots=None):
    import mailbox_report as MR
    import sector_report as SR
    from sources import server_databases
    roots = [str(cfg["_data_dir"])] + list(roots or default_roots())
    mail = []
    for b in MR.find_boxes(roots):
        try:
            r = MR.read_box(b)
            notread = sum(r["fails"].values()) if r.get("fails") else (r["counts"].get("failed") or 0)
            try:
                ch = MR.chart(r)
            except Exception:
                ch = ""
            mail.append({"name": r["name"], "path": b, "exported": r["n"], "outlook": r["counts"].get("items in Outlook"),
                         "folders": r["counts"].get("Folders"),
                         "notread": notread, "oldest": str(r["oldest"])[:10], "newest": str(r["newest"])[:10],
                         "on": r["exported_on"].strftime("%d/%m/%Y"), "verdict": r["verdict"][:80],
                         "stores": dict(r.get("stores") or {}), "chart": ch,
                         "notread_by_folder": [[f, n] for f, n in (r.get("fails") or {}).items()][:6],
                         "findings": [{"level": l, "what": w, "fix": f} for l, w, f in MR.diagnose(r)][:6]})
        except Exception as err:
            mail.append({"name": Path(b).name, "path": b, "error": str(err)[:160]})
    indexed = {}
    for label, path in server_databases(cfg):
        try:
            db = ro(path)
            indexed[label.upper()] = (db.execute("SELECT count(*) FROM messages").fetchone()[0],
                                      db.execute("SELECT count(*) FROM chunks").fetchone()[0])
            db.close()
        except Exception:
            pass
    sectors = {}
    for f in sector_folders(roots):
        try:
            s = SR.report_sector(f)
        except Exception as err:
            s = {"name": Path(f).name, "error": str(err)[:160]}
        s["path"] = f
        key = s["name"].upper()
        if key not in sectors or (s.get("docs") or 0) > (sectors[key].get("docs") or 0):
            sectors[key] = s
    rows = []
    for key, s in sorted(sectors.items()):
        reasons = sorted((s.get("reasons") or {}).items(), key=lambda kv: -kv[1])
        rows.append({"name": s["name"], "path": s["path"], "state": s.get("state"), "error": s.get("error"),
                     "listed": s.get("listed"), "docs": s.get("docs"), "text_mb": round(s.get("text_mb") or 0, 1),
                     "orig_gb": round(s.get("orig_gb") or 0, 1), "excluded": s.get("excluded"),
                     "share": round(100 * (s.get("docs") or 0) / s["text_bearing"]) if s.get("text_bearing") else None,
                     "skipped": reasons[:4], "indexed": indexed.get(key)})
    snap = {"made": datetime.now().strftime("%d/%m/%Y %H:%M"), "roots": roots, "mail": mail, "sectors": rows}
    p = snapshot_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return snap


_REFRESH = {"thread": None, "state": "idle", "started": "", "error": ""}


def start_refresh(cfg):
    """Recompute the export snapshot in the background (read-only on the exports)."""
    t = _REFRESH["thread"]
    if t and t.is_alive():
        return {"state": "running", "started": _REFRESH["started"]}

    def run():
        try:
            take_snapshot(cfg)
            _REFRESH["state"], _REFRESH["error"] = "done", ""
        except Exception as err:
            _REFRESH["state"], _REFRESH["error"] = "error", str(err)[:200]
    _REFRESH.update(state="running", started=datetime.now().strftime("%H:%M"), error="")
    _REFRESH["thread"] = threading.Thread(target=run, daemon=True)
    _REFRESH["thread"].start()
    return {"state": "running", "started": _REFRESH["started"]}


def mail_scope(cfg):
    """'own' (default): only the mailbox of this data folder, so a person running the app from their
    flash disk sees only their own export, even if another person's disk is plugged in.
    'all': every mailbox export found (the operator's laptop). Set PROPARCO_EVAL_SCOPE=all, or
    "eval_mail_scope": "all" in config/settings.json."""
    return (os.environ.get("PROPARCO_EVAL_SCOPE") or cfg.get("eval_mail_scope") or "own").lower()


def is_own(cfg, path):
    mine = os.path.realpath(str(cfg["_data_dir"]))
    p = os.path.realpath(path)
    return p == mine or p.startswith(mine + os.sep)


def mailbox_card(m):
    if m.get("error"):
        return f'<div class="mcard"><b>{esc(m["name"])}</b><p class="fl">Could not be read: {esc(m["error"])}</p></div>'
    ok = m["verdict"].startswith("ALL")
    tl = [(f"{(m['exported'] or 0):,}", "exported"), (f"{(m['outlook'] or 0):,}", "items in Outlook"),
          (f"{(m['notread'] or 0):,}", "not read")] + ([(f"{m['folders']:,}", "folders")] if m.get("folders") else []) + \
         [(f"{esc(m['oldest'])} → {esc(m['newest'])}", "period")]
    finds = "".join(f'<div class="find {"bad" if f["level"] == "bad" else "warn"}">{esc(f["what"])}<div class="fix">Fix: {esc(f["fix"])}</div></div>'
                    for f in m.get("findings") or [])
    nr = "".join(f"<tr><td>{esc(f)}</td><td>{n:,}</td></tr>" for f, n in m.get("notread_by_folder") or [])
    return (f'<div class="mcard"><div class="mh"><b>{esc(m["name"])}</b> <span class="{"ok" if ok else "fl"}">{esc(m["verdict"])}</span></div>'
            f'<div class="meta">{esc(m["path"])} · exported {esc(m["on"])}</div>'
            '<div class="tiles">' + "".join(f'<div class="tile"><div class="n">{v}</div><div class="l">{esc(l)}</div></div>' for v, l in tl) + '</div>'
            + (m.get("chart") or "")
            + (f'<h3>Diagnosis</h3>{finds}' if finds else '<p class="ok">No problem found.</p>')
            + (f'<h3>Not read, by folder</h3><table>{nr}</table>' if nr else '') + '</div>')


def load_snapshot(cfg):
    p = snapshot_path(cfg)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- page

def idx(s):
    i = s.get("indexed")
    return f"{i[0]:,} docs · {i[1]:,} pieces" if i else '<span class="fl">not indexed here</span>'


def pct(a, b):
    return f"{100 * a / b:.0f}%" if b else "–"


def build(cfg, embed=False):
    sq, sm, ch, cv, dt = search_quality(cfg), summaries(cfg), chat_answers(cfg), coverage(cfg), director_set()
    tiles = lambda items: '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="n">{esc(v)}</div><div class="l">{esc(l)}</div></div>' for v, l in items) + "</div>"
    P = []
    # 1. search
    if sq:
        P.append(f"""<section id="p1" class="panel"><h2>1 · Search quality</h2>
{tiles([(f"{sq['hit1']}%", "right document first"), (f"{sq['hit5']}%", "in the top 5"), (f"{sq['hitk']}%", f"in the top {sq['k']}"),
        (f"{sq['mrr']:.2f}", "MRR (1.00 = always first)")] + ([(f"{sq['search_s']:.2f} s", "per search (median)")] if sq.get('search_s') else []))}
<p class="meta">{esc(sq.get('n') or '?')} test questions on {esc(sq.get('db') or 'a sector')} · {esc(sq['source'])}{', ' + esc(sq['when']) if sq.get('when') else ''}.
{('Not found: ' + esc(', '.join(sq['misses'])) + '.') if sq.get('misses') else ''} The test questions were written from document file names.</p></section>""")
    else:
        P.append('<section id="p1" class="panel"><h2>1 · Search quality</h2><p class="none">No search test report found. Run: '
                 '<code>python3 -B scripts/eval_search.py --db &lt;sector&gt;/server.db --questions scripts/eval/questions_cross.json</code></p></section>')
    # 2 and 3. summaries and approvals
    if sm:
        rows = "".join(
            f"<tr><td>{esc(d['name'])}</td><td>{'<span class=\"fl\">held back</span>' if d['flagged'] else 'ready'}</td>"
            f"<td>{'yes' if d['fact'] else ''}</td><td>{d['history'] or ''}</td><td>{d['steps'] or ''}</td><td>{d['done'] or ''}</td>"
            f"<td>{d['refused'] or ''}</td><td>{('✓ after checking' if d['checked'] else '✓') if d['approved'] else ('waiting' if d['changed'] else 'nothing to update')}</td></tr>"
            for d in sm["deals"])
        P.append(f"""<section id="p2" class="panel"><h2>2 · Deal summaries</h2>
{tiles([(sm['total'], "deals summarised"), (sm['ready'], "ready to review"), (sm['flagged'], "held back for checking"),
        (sm['changed'], "with something new for Notion"), (sm['refused'], "items refused (not in the sources)")])}
<p class="meta">Added across all deals: {sm['history']} history entries, {sm['steps']} next steps, {sm['done']} Notion steps marked done.
{sm['retrieval']} of {sm['total']} made by retrieval (newest emails + relevant passages + documents). Refused items are claims whose names or numbers were not in any cited source.</p></section>
<section id="p3" class="panel"><h2>3 · Approvals</h2>
{tiles([(sm['approved'], "approved"), (sm['approved_checked'], "approved after checking a flag"), (sm['waiting'], "waiting for a decision")])}
<p class="meta">Approvals are recorded on this laptop. Nothing has been sent to Notion: the push will send approved updates only.</p>
<div class="scroll"><table><tr><th>Deal</th><th>Proposal</th><th>New fact</th><th>History</th><th>Steps</th><th>Done</th><th>Refused</th><th>Decision</th></tr>{rows}</table></div></section>""")
    else:
        P.append('<section id="p2" class="panel"><h2>2 · Deal summaries</h2><p class="none">No deal summaries yet in this mailbox.</p></section>')
    # 4. chat
    if ch:
        P.append(f"""<section id="p4" class="panel"><h2>4 · Chat answers</h2>
{tiles([(ch['answers'], "answers given"), (ch['deals'], "deals asked about"), (f"{ch['median_s']} s", "per answer (median)"),
        (ch['sources'], "sources per answer"), (ch['warned'], "answers with a warning")])}
<p class="meta">Every answer is checked against its sources; a warning means a name or number was not found in them. Includes archived conversations.</p></section>""")
    else:
        P.append('<section id="p4" class="panel"><h2>4 · Chat answers</h2><p class="none">No chat answers saved yet.</p></section>')
    # 5. coverage
    sec_rows = "".join(f"<tr><td>{esc(l)}</td><td>{n:,}</td><td>{c:,}</td></tr>" if n is not None else f"<tr><td>{esc(l)}</td><td colspan=2>unreadable</td></tr>"
                       for l, n, c in cv["sectors"])
    em = cv["emails"]
    P.append(f"""<section id="p5" class="panel"><h2>5 · Coverage</h2>
{tiles(([(f"{em[0]:,}", "emails indexed (this mailbox)")] if em else []) +
       [(f"{sum(n or 0 for _, n, _ in cv['sectors']):,}", "server documents indexed"), (len(cv['sectors']), "sectors searchable")] +
       ([(pct(*cv['matched']), "email threads matched to a deal")] if cv['matched'] else []))}
<div class="scroll"><table><tr><th>Sector</th><th>Documents</th><th>Pieces</th></tr>{sec_rows}</table></div>
<p class="meta">{f"Notion copy: {cv['notion'][0]} deals, synced {cv['notion'][1]}." if cv['notion'] else "No Notion copy found."}
Threads that match no deal are mostly internal mail (meetings, circulars, newsletters).</p></section>""")
    # 6 and 7. exports (from the snapshot)
    snap, running = load_snapshot(cfg), _REFRESH["state"] == "running"
    btn = ('<p class="meta">Refreshing the export figures (started ' + esc(_REFRESH["started"]) + '): this page updates by itself.</p>' if running else
           '<p class="meta"><button class="rb" onclick="fetch(\'/api/eval/refresh\').then(()=>location.reload())">Refresh export figures</button> '
           'reads every export on this laptop and on the plugged-in flash disks (a few minutes for the large sectors).'
           + (f' Last refresh failed: {esc(_REFRESH["error"])}' if _REFRESH["state"] == "error" else '') + '</p>')
    if snap:
        scope = mail_scope(cfg)
        shown_mail = snap["mail"] if scope == "all" else [m for m in snap["mail"] if is_own(cfg, m["path"])]
        hidden = len(snap["mail"]) - len(shown_mail)
        mrows = "".join(
            (f"<tr><td><b>{esc(m['name'])}</b><div class='meta'>{esc(m['path'])}</div></td><td colspan=6>could not be read: {esc(m['error'])}</td></tr>" if m.get("error") else
             f"<tr><td><b>{esc(m['name'])}</b><div class='meta'>{esc(m['path'])}</div></td><td>{(m['exported'] or 0):,}</td>"
             f"<td>{(m['outlook'] or 0):,}</td><td>{(m['notread'] or 0):,}</td><td>{esc(m['oldest'])} → {esc(m['newest'])}</td>"
             f"<td class='{'ok' if m['verdict'].startswith('ALL') else 'fl'}'>{esc(m['verdict'])}<div class='meta'>exported {esc(m['on'])}</div></td>"
             f"<td>{'<br>'.join(esc(x['what']) for x in m['findings'][:2]) or '–'}</td></tr>")
            for m in shown_mail)
        tot_e = sum(m.get("exported") or 0 for m in shown_mail); tot_o = sum(m.get("outlook") or 0 for m in shown_mail)
        scope_note = ("Showing every mailbox export found (operator view)." if scope == "all" else
                      "Showing only this mailbox's own export" + (f"; {hidden} other export(s) on plugged-in disks are not shown." if hidden else "."))
        if shown_mail:
            body6 = (tiles([(len(shown_mail), "mailbox export" + ("s" if len(shown_mail) > 1 else "")), (f"{tot_e:,}", "emails exported"),
                            (f"{tot_o:,}", "items held in Outlook"), (pct(tot_e, tot_o), "of Outlook's items exported")])
                     + "".join(mailbox_card(m) for m in shown_mail))
        else:
            body6 = ('<p class="none">This mailbox\'s own export (its acquisitions and export logs) is not in this data folder, '
                     'so there is nothing to show here. Run the export from this flash disk, then refresh.</p>')
        P.append(f"""<section id="p6" class="panel"><h2>6 · Email exports</h2>{body6}
<p class="meta">{esc(scope_note)} Snapshot of {esc(snap['made'])}.</p>{btn}</section>""")
        srows = "".join(
            (f"<tr><td><b>{esc(s['name'])}</b></td><td colspan=6>could not be read: {esc(s['error'])}</td></tr>" if s.get("error") else
             f"<tr><td><b>{esc(s['name'])}</b><div class='meta'>{esc(s['path'])}</div></td><td>{(s['listed'] or 0):,}</td><td>{(s['docs'] or 0):,}"
             f"{'<div class=meta>' + str(s['share']) + '% of text-bearing files</div>' if s.get('share') is not None else ''}</td>"
             f"<td>{s['orig_gb']} GB → {s['text_mb']} MB</td><td>{'<br>'.join(f'{esc(k)}: {v:,}' for k, v in s['skipped']) or '–'}</td>"
             f"<td>{(s['excluded'] or 0):,}</td><td>{idx(s)}</td></tr>")
            for s in snap["sectors"])
        P.append(f"""<section id="p7" class="panel"><h2>7 · Server extraction</h2>
{tiles([(len(snap['sectors']), "sectors extracted"), (f"{sum(s.get('listed') or 0 for s in snap['sectors']):,}", "files listed on the server"),
        (f"{sum(s.get('docs') or 0 for s in snap['sectors']):,}", "documents extracted"), (sum(1 for s in snap['sectors'] if s.get('indexed')), "indexed and searchable here")])}
<div class="scroll"><table><tr><th>Sector</th><th>Files listed</th><th>Documents</th><th>Original → text</th><th>Skipped, by reason</th><th>Left out (sensitive)</th><th>Indexed</th></tr>{srows}</table></div>
<p class="meta">Snapshot of {esc(snap['made'])}. Every skipped file is listed with its reason in the sector's logs; "left out" are documents excluded by name (CVs, IDs, payslips...).</p>{'' if snap['mail'] else btn}</section>""")
    else:
        P.append(f'<section id="p6" class="panel"><h2>6 · Email exports and 7 · Server extraction</h2><p class="none">No snapshot yet. '
                 f'Plug in the flash disks holding the exports, then refresh.</p>{btn}</section>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Evaluation</title><style>
:root{{--bg:#f3f6f7;--panel:#fff;--ink:#1d2b33;--muted:#5d6b73;--line:#d9e1e5;--accent:#0f5c6e;--warn:#c2410c;--tile:#eef3f4;--c1:#1971c2;--c2:#6741d9;--c3:#0c8599;--c4:#e8590c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12181b;--panel:#1a2226;--ink:#e4eaed;--muted:#9aa8af;--line:#2c383e;--accent:#6cc3d6;--warn:#f0a35a;--tile:#22303a}}}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:var(--bg);color:var(--ink)}}
header{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:12px 20px;border-bottom:1px solid var(--line)}}header h1{{margin:0;font-size:18px}}
a.nav{{color:var(--accent);font-weight:600;text-decoration:none;font-size:14px;border:1px solid currentColor;border-radius:8px;padding:3px 10px}}
main{{max-width:1150px;margin:0 auto;padding:16px 20px}}.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;margin-bottom:14px}}
.panel h2{{margin:0 0 10px;font-size:16px}}.tiles{{display:flex;flex-wrap:wrap;gap:10px}}
.tile{{background:var(--tile);border-radius:10px;padding:10px 14px;min-width:150px}}.tile .n{{font-size:26px;font-weight:700;color:var(--accent)}}.tile .l{{font-size:12.5px;color:var(--muted)}}
.meta{{color:var(--muted);font-size:13px;margin:10px 0 0}}.none{{color:var(--muted)}}.fl{{color:var(--warn);font-weight:600}}
.scroll{{overflow-x:auto;margin-top:10px}}table{{border-collapse:collapse;width:100%;font-size:13.5px}}th,td{{text-align:left;padding:6px 8px;border-top:1px solid var(--line)}}
code{{font-size:12.5px}}.ok{{color:#2f9e44;font-weight:600}}
.mcard{{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-top:12px}}.mcard .mh{{font-size:15px}}.mcard h3{{font-size:14px;margin:12px 0 6px}}
.find{{border-left:4px solid #e8590c;padding:4px 10px;margin:6px 0;font-size:13.5px}}.find.bad{{border-left-color:#c92a2a}}.find .fix{{color:var(--muted);font-size:13px}}
.ch{{width:100%;height:auto;margin-top:8px}}.ax{{font-size:10px;fill:var(--muted)}}.axl{{stroke:var(--line)}}.small{{font-size:12.5px;color:var(--muted)}}
.lg{{margin-right:10px}}.lg i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:middle}}button.rb{{font:inherit;font-weight:600;font-size:13px;padding:3px 10px;border-radius:8px;border:1px solid var(--accent);background:transparent;color:var(--accent);cursor:pointer}}</style>
{'<meta http-equiv="refresh" content="10">' if _REFRESH["state"] == "running" else ''}</head><body>
<header><h1>Evaluation</h1>{'' if embed else '<a class="nav" href="/deals">&larr; Deals</a><a class="nav" href="/">Chat &rarr;</a>'}
<span class="meta" style="margin:0">Read-only · built {datetime.now():%d/%m/%Y %H:%M} from what is saved on this laptop</span></header>
<main>{''.join(P)}</main></body></html>"""


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(HERE))
    from engine import load_config
    if "--snapshot" in sys.argv:
        s = take_snapshot(load_config())
        print(f"Export snapshot: {len(s['mail'])} mailbox export(s), {len(s['sectors'])} sector(s) -> {snapshot_path(load_config())}")
        sys.exit(0)
    out = load_config()["_data_dir"] / "evaluation.html"
    out.write_text(build(load_config()), encoding="utf-8")
    print(f"Evaluation page: {out}")
