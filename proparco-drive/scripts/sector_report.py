#!/usr/bin/env python3
"""
sector_report.py - verify extracted sector folders and write a simple report.

    python3 scripts/sector_report.py /media/me/STICK/ENI /media/me/STICK/MAS ...
    python3 scripts/sector_report.py --scan /media/me/STICK        (finds sector folders itself)

For each sector folder (with status.json, logs/, acquisitions/, sdrive-index.json):
status, files listed, original size, documents captured, text size, skip reasons,
sensitive exclusions (current rules), and the search database if present.
Read-only: nothing in the folders is changed. Writes sector-report-<date>.md.
"""
import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import embed as E  # noqa: E402  (exclusion rules and chunking, exactly as indexing uses them)

RETRIED = ("FileNotFoundError", "encrypted PDF", "could not read", "no permission")


def group(reason):
    r = reason.lower()
    if "scanned" in r or "text layer" in r: return "scanned PDF (needs OCR)"
    if "old format" in r: return "old Office format"
    if "file type not read" in r: return "no text (photos, archives, other types)"
    if "larger than" in r: return "over size limit"
    if "encrypted" in r or "password" in r: return "encrypted / password"
    if r.strip() == "no text": return "empty (no text)"
    return "damaged / unreadable"


def passes(folder):
    """Per-pass summaries. Permanent skips are counted once (not retried); retried reasons
    are taken from the last pass only (earlier failures were retried)."""
    out, perm, last = [], {}, {}
    logs = sorted(glob.glob(f"{folder}/logs/pass-*.log"), key=os.path.getmtime)
    for i, f in enumerate(logs):
        t = Path(f).read_text(encoding="utf-8", errors="replace")
        listed = re.search(r"Files listed:\s*([\d,]+)", t)
        read = re.search(r"(?m)^([\d,]+) documents read in", t)
        out.append({"listed": int(listed.group(1).replace(",", "")) if listed else 0,
                    "read": int(read.group(1).replace(",", "")) if read else 0})
        skips = {m.group(2).strip(): int(m.group(1).replace(",", ""))
                 for m in re.finditer(r"(?m)^\s+skipped\s+([\d,]+):\s*(.+)$", t)}
        for reason, n in skips.items():
            if not any(k in reason for k in RETRIED):
                perm[reason] = perm.get(reason, 0) + n
        if i == len(logs) - 1:
            last = {r: n for r, n in skips.items() if any(k in r for k in RETRIED)}
    reasons = {}
    for r, n in list(perm.items()) + list(last.items()):
        reasons[group(r)] = reasons.get(group(r), 0) + n
    return out, reasons


def report_sector(folder):
    folder = Path(folder)
    s = {"name": folder.name}
    try:
        st = json.loads((folder / "status.json").read_text(encoding="utf-8-sig"))
        s["state"] = st.get("state", "?")
    except Exception:
        s["state"] = "no status file"
    s["passes"], s["reasons"] = passes(folder)
    s["listed"] = max((p["listed"] for p in s["passes"]), default=0)

    norm = lambda p: (p or "").replace("/", "\\").lower()
    sizes = {}
    try:
        for p, stamp in json.loads((folder / "sdrive-index.json").read_text(encoding="utf-8")).items():
            head = str(stamp).split(":")[0]
            if head.isdigit():
                sizes[norm(p)] = int(head)
    except Exception:
        pass
    rx = re.compile("|".join(f"(?:{p})" for p in E.SENSITIVE_DOCS), re.I)
    got, docs, chars, excluded, chunks = set(), 0, 0, 0, 0
    for f in glob.glob(f"{folder}/acquisitions/*/acquisition.json"):
        try:
            msgs = json.loads(Path(f).read_text(encoding="utf-8"))["messages"]
        except Exception:
            continue
        for m in msgs:
            docs += 1
            body = m.get("body") or ""
            chars += len(body)
            got.add(norm(m.get("sourcePath")))
            if rx.search(E.doc_location({"subject": m.get("subject"), "body": body})):
                excluded += 1
            else:
                chunks += min(E.MAX_CHUNKS_PER_DOCUMENT, len(E.chunk_text(re.sub(r"\n{3,}", "\n\n", body).strip())))
    s["docs"], s["chars"], s["excluded"], s["chunks"] = docs, chars, excluded, chunks
    s["text_mb"] = sum(os.path.getsize(p) for p in glob.glob(f"{folder}/acquisitions/**/*", recursive=True)
                       if os.path.isfile(p)) / 1e6
    s["orig_gb"] = sum(sizes.values()) / 1e9
    s["captured_gb"] = sum(v for k, v in sizes.items() if k in got) / 1e9
    no_text = s["reasons"].get("no text (photos, archives, other types)", 0)
    s["text_bearing"] = max(1, s["listed"] - no_text)

    db = folder / "db" / "server.db"
    if db.exists():
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
            s["db"] = {"mb": db.stat().st_size / 1e6,
                       "ok": con.execute("pragma quick_check").fetchone()[0],
                       "docs": con.execute("select count(*) from messages").fetchone()[0],
                       "chunks": con.execute("select count(*) from chunks").fetchone()[0],
                       "vectors": con.execute("select count(*) from vec_chunks_rowids").fetchone()[0]}
        except Exception as e:
            s["db"] = {"error": str(e)[:80]}
    return s


def render(sectors):
    L = [f"# Server extraction report ({datetime.now():%d %B %Y, %H:%M})", "",
         "Figures computed from each sector's status file, pass logs, extraction index and extracted text.", "",
         "| Sector | Status | Files | Original | Captured | Of text-bearing | By volume | Text | Excluded | Indexed |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for s in sectors:
        d = s.get("db")
        idx = (f"✅ {d['chunks']:,} chunks, {d['mb']:,.0f} MB" if d and "chunks" in d and d["chunks"] == d["vectors"] and d["ok"] == "ok"
               else "⚠️ check" if d else f"no (~{s['chunks']:,} chunks, ~{s['chunks']/1.9/3600:.1f} h)")
        vol = f"{100*s['captured_gb']/s['orig_gb']:.0f}%" if s["orig_gb"] else "?"
        L.append(f"| {s['name']} | {s['state']} | {s['listed']:,} | {s['orig_gb']:,.1f} GB | {s['docs']:,} | "
                 f"{100*s['docs']/s['text_bearing']:.0f}% | {vol} | {s['text_mb']:,.0f} MB | {s['excluded']:,} | {idx} |")
    tot = lambda k: sum(s[k] for s in sectors)
    L += ["", f"**Total:** {tot('listed'):,} files, {tot('orig_gb'):,.1f} GB → {tot('docs'):,} documents, "
              f"{tot('text_mb'):,.0f} MB of text ({100*tot('text_mb')/1e3/max(tot('orig_gb'),1e-9):.1f}% of the original size).", "",
          "## Not captured, by reason", "", "| Sector | " + " | ".join(
              ["scanned PDF", "old Office", "no text", "over size", "encrypted", "damaged", "empty"]) + " |",
          "|---|---|---|---|---|---|---|---|"]
    keys = ["scanned PDF (needs OCR)", "old Office format", "no text (photos, archives, other types)", "over size limit",
            "encrypted / password", "damaged / unreadable", "empty (no text)"]
    for s in sectors:
        L.append(f"| {s['name']} | " + " | ".join(f"{s['reasons'].get(k, 0):,}" for k in keys) + " |")
    L += ["", "## Passes (network retries)", ""]
    for s in sectors:
        L.append(f"- **{s['name']}**: " + ", ".join(f"pass {i+1}: {p['read']:,} read" for i, p in enumerate(s["passes"])))
    L += ["", "Notes: 'Of text-bearing' excludes files with no text to capture (photos, archives...). "
              "'By volume' is the original size of captured files over the sector's total. "
              "'Excluded' = sensitive documents left out of search (CVs, IDs, KYC, expense papers...), current rules."]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="Verify extracted sector folders and write a simple report.")
    ap.add_argument("folders", nargs="*", help="sector folders")
    ap.add_argument("--scan", help="find sector folders (with acquisitions/ and status.json) under this path")
    ap.add_argument("--out", default=".", help="folder for the report file (default: current folder)")
    args = ap.parse_args()
    folders = list(args.folders)
    if args.scan:
        for st in sorted(glob.glob(f"{args.scan}/**/status.json", recursive=True)):
            f = Path(st).parent
            if (f / "acquisitions").is_dir() and (f / "logs").is_dir():
                folders.append(str(f))
    if not folders:
        sys.exit("No sector folders given or found.")
    sectors = []
    for f in folders:
        print(f"checking {f} ...", file=sys.stderr)
        sectors.append(report_sector(f))
    text = render(sectors)
    out = Path(args.out) / f"sector-report-{datetime.now():%Y%m%d-%H%M}.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nSaved: {out}", file=sys.stderr)


if __name__ == "__main__":
    main()