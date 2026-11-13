#!/usr/bin/env python3
"""
repair_xlsx_text.py - make the spreadsheet text of a sector readable by the chat WITHOUT the server.

    python3 scripts/repair_xlsx_text.py <sector folder>          e.g. ~/proparco-backup/INV

The old spreadsheet reader wrote each row as values joined by " | ", dropped empty cells and kept no
link to the column headings. This script reads that old text from the sector's acquisition.json
files and rewrites each spreadsheet in the format of the new reader:
    <table title> · <row name> · FY2020 Actual: 0.409 · FY2021 Actual: 0.604 · ...
  - date rows (Excel numbers such as 44196) become FY2020...; a row of Actual/Budget/Forecast is attached;
  - side-by-side tables are split by their titles and row names;
  - a COMPLETE row (as many values as periods) is aligned with its periods;
  - a row with GAPS cannot be aligned from the old text (where the gaps were is lost): it is kept,
    marked "not aligned", never guessed. A re-read from the server aligns it later;
  - values beyond the tables (e.g. CAGR) and commentary are kept as "beside" notes.
The result is a NEW run folder in the sector's acquisitions (named with today's date and time, so it
counts as the newest version): index only that folder with
    embed.py --documents "<that folder>"
and the repaired spreadsheets replace their old text; nothing else is touched. Read-only on the
existing runs.
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from drive_acquire import _fmt, _period, PERIOD_KIND  # noqa: E402  (same rules as the new reader)

REPAIR_TAG = "repaired from the extracted text (table-aware v2 format; rows with gaps not aligned)"


def _is_num(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _segments(cells, is_value):
    """[(label or "", [values])] - a new segment starts at each text cell that is not a value."""
    segs = []
    for c in cells:
        if is_value(c):
            if not segs:
                segs.append(["", []])
            segs[-1][1].append(c)
        else:
            segs.append([c, []])
    return segs


def repair_sheet(lines, stats):
    out, blocks, side = [], [], []     # blocks: [{"title", "periods": [...]}]; side: headings beyond the tables
    for line in lines:
        cells = [c.strip() for c in line.split(" | ")]
        periods = [c for c in cells if _period(c)]
        if len(periods) >= 3:                                   # a heading row
            blocks, side = [], []
            for label, vals in _segments(cells, lambda c: bool(_period(c))):
                if vals:
                    blocks.append({"title": label, "periods": [_period(v) for v in vals]})
                elif blocks:
                    side.append(label)
            continue
        if blocks and sum(1 for c in cells if PERIOD_KIND.match(c)) >= 3:      # Actual / Budget / Forecast
            segs = [s for s in _segments(cells, lambda c: bool(PERIOD_KIND.match(c))) if s[1]]
            for b, (label, kinds) in zip(blocks, segs):
                if len(kinds) == len(b["periods"]):
                    b["periods"] = [f"{p} {k}" for p, k in zip(b["periods"], kinds)]
            continue
        if not blocks:
            out.append(line)
            continue
        segs = _segments(cells, _is_num)
        beside = []
        if segs and not segs[-1][1] and len(segs) > 1:          # trailing commentary
            beside.append(segs.pop()[0])
        for i, (label, vals) in enumerate(segs):
            if i >= len(blocks):
                beside.extend(([label] if label else []) + [_fmt(v) for v in vals])
                continue
            b = blocks[i]; n = len(b["periods"])
            head = " · ".join(x for x in (b["title"], label) if x)
            if not vals:
                out.append(head)
            elif len(vals) >= n:
                out.append(" · ".join([head] + [f"{p}: {_fmt(v)}" for p, v in zip(b["periods"], vals)]))
                extra = vals[n:]
                if extra:
                    beside.extend(f"{side[k]}: {_fmt(v)}" if k < len(side) else _fmt(v) for k, v in enumerate(extra))
                stats["aligned"] += 1
            else:
                out.append(f"{head} · (not aligned: {len(vals)} values for {n} periods; see the original) "
                           + " | ".join(_fmt(v) for v in vals))
                stats["not_aligned"] += 1
        if beside:
            out.append("  (beside: " + " | ".join(beside) + ")")
    return out


def repair_text(body, stats):
    i = body.find("[Sheet ")
    if i < 0:
        return body
    head, rest = body[:i], body[i:]
    out, sheet = [], []
    for line in rest.splitlines():
        if line.startswith("[Sheet ") or line == "(further rows not read)":
            if sheet:
                out.extend(repair_sheet(sheet, stats)); sheet = []
            out.append(line)
        else:
            sheet.append(line)
    if sheet:
        out.extend(repair_sheet(sheet, stats))
    return head + "\n".join(out)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sector = Path(os.path.expanduser(sys.argv[1]))
    acq = sector / "acquisitions" if (sector / "acquisitions").is_dir() else sector
    runs = sorted(p.parent for p in acq.rglob("acquisition.json"))
    if not runs:
        sys.exit(f"No acquisition.json under {acq}")
    newest = {}                                      # entryId -> newest version (runs are in date order)
    for run in runs:
        try:
            msgs = json.loads((run / "acquisition.json").read_text(encoding="utf-8-sig")).get("messages") or []
        except (OSError, ValueError):
            print(f"  skipped an unreadable run: {run.name}")
            continue
        for m in msgs:
            if m.get("kind") == "document" and str(m.get("subject", "")).lower().endswith((".xlsx", ".xlsm")):
                newest[m.get("entryId")] = m
    stats = {"spreadsheets": len(newest), "repaired": 0, "already": 0, "aligned": 0, "not_aligned": 0}
    out_msgs = []
    for eid, m in newest.items():
        body = m.get("body") or ""
        if " · FY" in body or "not aligned" in body:  # already in the new format (re-read or repaired)
            stats["already"] += 1
            continue
        try:
            new = repair_text(body, stats)
        except Exception as err:                    # one odd spreadsheet never stops the run: left as it was
            stats.setdefault("failed", []).append(f"{m.get('subject')} ({err.__class__.__name__})")
            continue
        if new != body:
            out_msgs.append(dict(m, body=new, repair=REPAIR_TAG))
            stats["repaired"] += 1
    print(f"Spreadsheets: {stats['spreadsheets']:,} | repaired: {stats['repaired']:,} | already in the new format: {stats['already']:,}")
    print(f"Rows aligned with their periods: {stats['aligned']:,} | rows with gaps marked 'not aligned': {stats['not_aligned']:,}")
    if stats.get("failed"):
        print(f"Left as they were (could not be repaired): {len(stats['failed'])}: " + "; ".join(stats["failed"][:10])
              + (" ..." if len(stats["failed"]) > 10 else ""))
    if not out_msgs:
        print("Nothing to write.")
        return
    name = f"SDRIVE-{datetime.now():%Y-%m-%d_%H-%M-%S}-repair-xlsx-001"
    folder = acq / name
    folder.mkdir(parents=True)
    (folder / "acquisition.json").write_text(json.dumps({"source": "s-drive", "acquisitionMethod": REPAIR_TAG,
                                                         "acquiredAt": datetime.now().isoformat(timespec="seconds"),
                                                         "messages": out_msgs}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Written: {folder}")
    print(f"Next (index only these spreadsheets):  embed.py --documents \"{folder}\"")


if __name__ == "__main__":
    main()
