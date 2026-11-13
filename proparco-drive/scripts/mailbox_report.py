#!/usr/bin/env python3
"""
mailbox_report.py - one page showing every mailbox export: what was captured, what went wrong, the fix.

    python3 -B scripts/mailbox_report.py [folder ...]       (default: ~/proparco-work and plugged-in sticks)
    -> writes mailbox-report.html next to this program folder and opens it

A mailbox is any folder holding acquisitions/ (the exported emails) and logs/mail-export-*.log.
Read-only: nothing in the exports is changed. Works offline (no external scripts or fonts).

For each mailbox: emails exported, period covered, the export's own verdict and counts, a monthly
chart split by mailbox/archive, the failures grouped by folder and reason, and a diagnosis:
  - "connection to Outlook lost" (0x800706BA / 0x800706BE / 0x80010108): re-run, it retries only those
  - starts ~12 (or ~6) months before the export: Outlook's offline window ("Mail to keep offline")
  - only an archive exported: the main mailbox was not selected
  - shared mailboxes included (an address that is not the person's own)
  - older years thin compared with the last year: older mail probably in an (Online) Archive
"""
import collections
import glob
import html
import json
import os
import re
import sys
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

CONNECTION_LOST = ("800706BA", "800706BE", "80010108", "RPC")
DATE_RE = re.compile(r"mail-export-(\d{8})-(\d{4})\.log$")


def num(text):
    d = re.sub(r"\D", "", text or "")
    return int(d) if d else 0


def find_boxes(roots):
    seen = set()
    for r in roots:
        for a in glob.glob(os.path.join(r, "**", "acquisitions"), recursive=True):
            box = os.path.dirname(a)
            if glob.glob(os.path.join(box, "logs", "mail-export-*.log")):
                real = os.path.realpath(box)
                if real not in seen:
                    seen.add(real)
                    yield box


def read_box(box):
    months, stores, n, dates = collections.Counter(), collections.Counter(), 0, []
    for f in glob.glob(os.path.join(box, "acquisitions", "*", "acquisition.json")):
        try:
            msgs = json.load(open(f, encoding="utf-8-sig")).get("messages") or []
        except Exception:
            continue
        for m in msgs:
            if (m.get("kind") or "email") != "email":
                continue
            n += 1
            store = ((m.get("folderPath") or m.get("folder") or "?").split("\\")[0]) or "?"
            stores[store] += 1
            t = (m.get("receivedTime") or "")[:10]
            if t:
                dates.append(t)
                months[(t[:7], store)] += 1
    logs = sorted(glob.glob(os.path.join(box, "logs", "mail-export-*.log")))
    log = open(logs[-1], encoding="utf-8", errors="replace").read() if logs else ""
    m = DATE_RE.search(logs[-1]) if logs else None
    exported_on = datetime.strptime(m.group(1), "%Y%m%d") if m else datetime.now()
    counts = {}
    line = re.findall(r"Folders:[^\n]*", log)
    if line:
        for k, v in re.findall(r"(Folders|items in Outlook|exported now|already exported|failed):\s*([^|\n]*)", line[-1]):
            counts[k] = num(v)
    verdict = (re.findall(r"(ALL ITEMS ACCOUNTED FOR|ACCOUNTED FOR WITH EXCEPTIONS|STOPPED[^\n]*)", log) or ["no verdict: export not finished?"])[-1]
    unreadable = re.findall(r"could not be read[^\n]*\n((?:\s+[^\n]+\n){1,6})", log, re.I)
    fails, reasons, first_fail = collections.Counter(), collections.Counter(), {}
    for ff in glob.glob(os.path.join(box, "logs", "mail-failed-*.txt")):
        for row in open(ff, encoding="utf-8", errors="replace").read().splitlines()[1:]:
            parts = row.split("\t")
            if len(parts) < 2:
                continue
            folder, reason = parts[0], parts[1]
            fails[folder] += 1
            code = re.search(r"0x[0-9A-Fa-f]{8}", reason)
            reasons["connection to Outlook lost" if any(c in reason.upper() for c in CONNECTION_LOST)
                    else (code.group(0) if code else reason[:60])] += 1
            at = re.search(r"item (\d+) of (\d+)", reason)
            if at and folder not in first_fail:
                first_fail[folder] = (int(at.group(1)), int(at.group(2)))
    dates.sort()
    return {"box": box, "name": Path(box).name if Path(box).name not in ("proparco-data",) else Path(box).parent.name,
            "n": n, "oldest": dates[0] if dates else "", "newest": dates[-1] if dates else "",
            "months": months, "stores": stores, "exported_on": exported_on, "counts": counts, "verdict": verdict,
            "unreadable": [u.strip() for u in unreadable], "fails": fails, "reasons": reasons, "first_fail": first_fail}


def diagnose(b):
    """Plain-language findings, each (level, what, fix)."""
    out = []
    own = max(b["stores"], key=b["stores"].get) if b["stores"] else ""
    if b["reasons"].get("connection to Outlook lost"):
        where = "; ".join(f"{f} from item {i:,} of {t:,}" for f, (i, t) in list(b["first_fail"].items())[:3])
        out.append(("bad", f"Connection to Outlook lost during the export ({b['reasons']['connection to Outlook lost']:,} items not read: {where}).",
                    "Re-run the export with the laptop on power, sleep off, Outlook open and untouched: it skips what was done and retries only these."))
    other = [r for r in b["reasons"] if r != "connection to Outlook lost"]
    if other:
        out.append(("warn", "Other read errors: " + ", ".join(f"{r} ({b['reasons'][r]:,})" for r in other[:4]) + ".",
                    "Re-run once; if they persist, these items are protected or damaged in Outlook itself."))
    stores = list(b["stores"])
    if stores and all("archive" in s.lower() for s in stores):
        out.append(("bad", "Only an archive was exported: the main mailbox is missing.",
                    "Re-run and check the list shows the main mailbox as well as the archive before pressing Enter."))
    shared = [s for s in stores if "@" in s and s != own]
    if shared:
        out.append(("info", "Includes shared mailbox(es): " + ", ".join(f"{s} ({b['stores'][s]:,})" for s in shared) + ".",
                    "Fine to keep, but count the person's own mail without them when comparing mailboxes."))
    if b["oldest"]:
        span = (b["exported_on"] - datetime.strptime(b["oldest"], "%Y-%m-%d")).days
        for months, days in ((12, 365), (6, 182), (3, 91)):
            if abs(span - days) <= 10:
                out.append(("warn", f"The oldest email is about {months} months before the export: Outlook only keeps the last {months} months on this laptop.",
                            "In Outlook: File > Account Settings > Account Settings > the account > Change > 'Mail to keep offline' = All; let it download, then re-run (adds only the older mail)."))
                break
        else:
            per_year = collections.Counter()
            for (mth, _), c in b["months"].items():
                per_year[mth[:4]] += c
            last = str(b["exported_on"].year)
            prev = [per_year[str(y)] for y in range(b["exported_on"].year - 4, b["exported_on"].year - 1)]
            recent = per_year[last] + per_year[str(b["exported_on"].year - 1)]
            if span < 120:
                out.append(("info", f"All the mail is from the last {max(1, span // 7)} weeks.",
                            "Expected for a recent joiner; otherwise check Outlook's 'Mail to keep offline' setting."))
            elif prev and sum(prev) > 0 and recent and max(prev) < recent / 6:
                out.append(("warn", "Older years are thin compared with the last two: older mail is probably in an archive that was not included.",
                            "In Outlook, look for 'Online Archive' or old .pst files; open or expand them, then re-run and check they are in the list."))
    if b["unreadable"]:
        out.append(("warn", "Mailboxes or folders Outlook could not open: " + " | ".join(b["unreadable"])[:200],
                    "Usually shared or delegated mailboxes without access; fine to leave out unless they matter."))
    if not out and b["verdict"].startswith("ALL"):
        out.append(("good", "Complete: every item Outlook held was exported.", "Nothing to do."))
    return out


def chart(b, width=640, height=150):
    """Monthly emails for the last 36 months up to the export, stacked by mailbox/archive (inline SVG)."""
    end = b["exported_on"].replace(day=1)
    months = []
    y, m = end.year, end.month
    for _ in range(36):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    stores = [s for s, _ in b["stores"].most_common(4)]
    colors = ["var(--c1)", "var(--c2)", "var(--c3)", "var(--c4)"]
    totals = [sum(b["months"].get((mo, s), 0) for s in b["stores"]) for mo in months]
    top = max(totals + [1])
    bw = (width - 40) / len(months)
    bars = []
    for i, mo in enumerate(months):
        yb = height - 20
        for j, s in enumerate(stores):
            c = b["months"].get((mo, s), 0)
            h = (height - 30) * c / top
            if c:
                bars.append(f'<rect x="{30 + i * bw:.1f}" y="{yb - h:.1f}" width="{bw - 2:.1f}" height="{h:.1f}" fill="{colors[j]}">'
                            f'<title>{mo} · {html.escape(s)}: {c:,}</title></rect>')
            yb -= h
        if i % 6 == 0:
            bars.append(f'<text x="{30 + i * bw:.1f}" y="{height - 5}" class="ax">{mo}</text>')
    older = b["n"] - sum(totals)
    legend = " ".join(f'<span class="lg"><i style="background:{colors[j]}"></i>{html.escape(s)} ({b["stores"][s]:,})</span>'
                      for j, s in enumerate(stores))
    return (f'<svg viewBox="0 0 {width} {height}" class="ch" role="img"><text x="0" y="12" class="ax">{top:,}</text>'
            f'<line x1="28" y1="{height - 20}" x2="{width}" y2="{height - 20}" class="axl"/>{"".join(bars)}</svg>'
            f'<div class="small">{legend}{f" · {older:,} emails older than the 36 months shown" if older > 0 else ""}</div>')


def page(boxes):
    cards = []
    for b in boxes:
        c = b["counts"]
        kpis = [("exported", f'{b["n"]:,}'), ("items in Outlook", f'{c.get("items in Outlook", 0):,}'),
                ("not read", f'{c.get("failed", 0):,}'), ("folders", f'{c.get("Folders", 0):,}'),
                ("period", f'{b["oldest"]} → {b["newest"]}' if b["oldest"] else "?")]
        diag = "".join(f'<li class="{lv}"><b>{html.escape(w)}</b><br><span class="fix">Fix: {html.escape(f)}</span></li>'
                       for lv, w, f in diagnose(b))
        fails = "".join(f"<tr><td>{html.escape(f)}</td><td>{n:,}</td></tr>" for f, n in b["fails"].most_common(5))
        vclass = "good" if b["verdict"].startswith("ALL") else "warn"
        cards.append(f"""<section class="card"><h2>{html.escape(b['name'])} <span class="badge {vclass}">{html.escape(b['verdict'][:40])}</span></h2>
<div class="small">{html.escape(b['box'])} · exported {b['exported_on']:%d/%m/%Y}</div>
<div class="kpis">{''.join(f'<div><b>{v}</b><span>{k}</span></div>' for k, v in kpis)}</div>
{chart(b)}
<h3>Diagnosis</h3><ul class="diag">{diag}</ul>
{f'<h3>Not read, by folder</h3><table>{fails}</table>' if fails else ''}</section>""")
    total = sum(b["n"] for b in boxes)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mailbox exports</title><style>
:root{{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e2e1dc;--good:#2f9e44;--warn:#e8590c;--bad:#c92a2a;--info:#1971c2;
--c1:#1971c2;--c2:#6741d9;--c3:#0c8599;--c4:#e8590c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--card:#20201e;--ink:#ecebe6;--mute:#a3a29b;--line:#383732}}}}
body{{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--ink)}}main{{max-width:1100px;margin:auto;padding:18px}}
h1{{font-size:21px;margin:4px 0}}h2{{font-size:18px;margin:0 0 2px}}h3{{font-size:14px;margin:14px 0 4px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}}
.small{{font-size:12.5px;color:var(--mute)}}.kpis{{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}}
.kpis div{{border:1px solid var(--line);border-radius:8px;padding:6px 10px;min-width:110px}}.kpis b{{display:block;font-size:17px}}.kpis span{{font-size:12px;color:var(--mute)}}
.badge{{font-size:12px;padding:2px 8px;border-radius:20px;color:#fff;vertical-align:middle}}.badge.good{{background:var(--good)}}.badge.warn{{background:var(--warn)}}
.ch{{width:100%;height:auto;margin-top:6px}}.ax{{font-size:10px;fill:var(--mute)}}.axl{{stroke:var(--line)}}
.lg{{margin-right:12px}}.lg i{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px}}
.diag{{list-style:none;padding:0;margin:0}}.diag li{{border-left:4px solid var(--line);padding:4px 10px;margin:6px 0}}
.diag li.bad{{border-color:var(--bad)}}.diag li.warn{{border-color:var(--warn)}}.diag li.good{{border-color:var(--good)}}.diag li.info{{border-color:var(--info)}}
.fix{{color:var(--mute);font-size:13.5px}}table{{border-collapse:collapse;font-size:13px}}td{{border-top:1px solid var(--line);padding:3px 10px 3px 0}}
</style></head><body><main><h1>Mailbox exports</h1>
<div class="small">{len(boxes)} mailbox(es), {total:,} emails exported · report made {datetime.now():%d/%m/%Y %H:%M} · read-only: nothing in the exports was changed</div>
{''.join(cards)}</main></body></html>"""


def main():
    roots = sys.argv[1:] or [os.path.expanduser("~/proparco-work")] + glob.glob("/media/*/*") + glob.glob("/media/*")
    boxes = [read_box(b) for b in find_boxes(roots)]
    boxes = [b for b in boxes if b["n"]]
    if not boxes:
        sys.exit("No mailbox exports found (a folder with acquisitions/ and logs/mail-export-*.log).")
    out = Path(__file__).resolve().parent.parent / "mailbox-report.html"
    out.write_text(page(boxes), encoding="utf-8")
    for b in boxes:
        print(f"  {b['name']:28} {b['n']:7,} emails | {b['verdict'][:32]:32} | " +
              "; ".join(w[:70] for _, w, _ in diagnose(b))[:160])
    print(f"\nReport: {out}")
    try:
        webbrowser.open(out.as_uri())
    except Exception:
        pass


if __name__ == "__main__":
    main()
