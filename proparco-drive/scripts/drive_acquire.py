#!/usr/bin/env python3
"""
drive_acquire.py - read documents from shared drives (e.g. Proparco's server shares), READ-ONLY,
into the same acquisition format as the emails, so they become searchable with them.

  1. Inventory (fast, reads no file contents): what is there, how much, what looks sensitive
       start.bat drive_acquire.py --inventory --path "\\\\server\\Metier" --path "\\\\server\\Agence"
  2. Extraction (incremental: a rerun only reads new or changed files; saved in batches)
       start.bat drive_acquire.py --path "\\\\server\\Metier" --path "\\\\server\\Agence"
  Exclusions: --exclude KYC --exclude passport, or "sdrive_exclude": [...] in settings.json

Nothing on the drive is ever written, moved or deleted: files are only opened for reading.
Text is extracted from: Word (.docx), Excel (.xlsx), PowerPoint (.pptx), PDF (needs pypdf),
.txt .md .csv .json .xml .html. Old formats (.doc .xls .ppt) and scans are listed as skipped.
Each document is grouped under its top-level folder (one "thread" per project folder).
Then run embed.py to make the documents searchable.
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import load_config  # noqa: E402

PDF_MAX_PAGES = 300        # a single huge PDF must not stall the run
PDF_MAX_SECONDS = 60

TEXT_EXT = {".txt", ".md", ".csv", ".json", ".xml", ".log"}
HTML_EXT = {".html", ".htm"}
OLD_EXT = {".doc", ".xls", ".ppt", ".msg", ".rtf"}


def read_text_file(path):
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def xml_text(data, para_tags=("p",)):
    """Plain text from an Office XML part: one line per paragraph."""
    text = data.decode("utf-8", errors="replace")
    for tag in para_tags:
        text = re.sub(rf"</(\w+:)?{tag}>", "\n", text)
    text = re.sub(r"<(\w+:)?tab\b[^>]*/?>", "\t", text)
    text = re.sub(r"<(\w+:)?(br|cr)\b[^>]*/?>", "\n", text)     # <a:br>, <a:br/>, <w:br/>, <w:cr/>
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def docx_text(path):
    with zipfile.ZipFile(path) as z:
        parts = [n for n in z.namelist()
                 if re.match(r"word/(document|header\d*|footer\d*|footnotes)\.xml$", n)]
        parts.sort(key=lambda n: (n != "word/document.xml", n))
        return "\n".join(xml_text(z.read(n)) for n in parts)


def pptx_text(path):
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        return "\n\n".join(f"[Slide {i}]\n" + xml_text(z.read(n))
                           for i, n in enumerate(slides, 1))


def xlsx_text(path, max_rows=2000):
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        names = {}
        if "xl/workbook.xml" in z.namelist():
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            for i, s in enumerate(wb.iter(f"{{{ns['m']}}}sheet"), 1):
                names[i] = s.get("name")
        out = []
        sheets = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for n in sheets:
            idx = int(re.findall(r"\d+", n)[-1])
            out.append(f"[Sheet {names.get(idx, idx)}]")
            root = ET.fromstring(z.read(n))
            for r_i, row in enumerate(root.iter(f"{{{ns['m']}}}row")):
                if r_i >= max_rows:
                    out.append("(further rows not read)")
                    break
                cells = []
                for c in row.findall("m:c", ns):
                    v = c.find("m:v", ns)
                    t = c.get("t")
                    if t == "s" and v is not None and v.text and v.text.isdigit():
                        val = shared[int(v.text)] if int(v.text) < len(shared) else ""
                    elif t == "inlineStr":
                        val = "".join(x.text or "" for x in c.iter(f"{{{ns['m']}}}t"))
                    else:
                        val = v.text if v is not None and v.text else ""
                    if val:
                        cells.append(val.strip())
                if cells:
                    out.append(" | ".join(cells))
        return "\n".join(out)


def pdf_text(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "PDF reader (pypdf) not available"
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):          # most "encrypted" PDFs only restrict printing/copying
                return None, "password-protected PDF"
        except Exception as err:
            if "cryptography" in str(err) or "AES" in str(err):
                return None, "encrypted PDF (encryption library missing on this drive)"
            return None, "password-protected PDF"
    pages, started, note = [], time.time(), ""
    total = len(reader.pages)
    for i, page in enumerate(reader.pages, 1):
        if i > PDF_MAX_PAGES or time.time() - started > PDF_MAX_SECONDS:
            note = f"\n[long PDF: first {i - 1} of {total} pages read]"
            break
        try:
            pages.append(f"[Page {i}]\n" + (page.extract_text() or ""))
        except Exception:
            pages.append(f"[Page {i}] (text could not be read)")
    text = "\n".join(pages) + note
    if len(re.sub(r"\[Page \d+\]|\s", "", text)) < 20:
        return None, "no text layer (scanned PDF; would need OCR)"
    return text, None


SENSITIVE = re.compile(r"(kyc|passport|passeport|pi[eè]ce.d.identit|carte.d.identit|\bcni\b|"
                       r"\bcv\b|curriculum|\bhr\b|\brh\b|ressources.humaines|salaire|salary|"
                       r"payroll|paie|bulletin|m[eé]dical|medical|sant[eé]|confidenti|password|"
                       r"mot.de.passe|sanction|compliance|conformit)", re.I)


def office_properties(path):
    """Author, title and dates stored inside Word/Excel/PowerPoint files (docProps/core.xml)."""
    if path.suffix.lower() not in (".docx", ".xlsx", ".pptx"):
        return {}
    try:
        with zipfile.ZipFile(path) as z:
            if "docProps/core.xml" not in z.namelist():
                return {}
            xml = z.read("docProps/core.xml").decode("utf-8", errors="replace")
    except (OSError, zipfile.BadZipFile):
        return {}
    props = {}
    for key, tag in (("title", "dc:title"), ("author", "dc:creator"),
                     ("last_modified_by", "cp:lastModifiedBy"),
                     ("created", "dcterms:created"), ("modified", "dcterms:modified")):
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml, re.S)
        if m and m.group(1).strip():
            value = html.unescape(m.group(1).strip())
            if key == "title" and value.lower() in {"powerpoint presentation", "présentation powerpoint",
                                                     "document", "microsoft word document"}:
                continue                 # application defaults, not real titles
            props[key] = value
    return props


def share_label(root):
    """'\\\\server\\Metier' -> 'Metier'; 'S:\\' -> 'S'."""
    parts = [p for p in re.split(r"[\\/]+", str(root)) if p and not p.endswith(":")]
    return parts[-1] if parts else str(root).rstrip(":\\/") or "drive"


DENIED = {}      # share -> list of folders the current user may not open


def walk(roots, excludes):
    """All files under the roots: (share, root, path, size, mtime). Folders the user may not
    open are recorded in DENIED (never bypassed): access is decided by the server."""
    for root in roots:
        label = share_label(root)

        def note(err, label=label):
            if isinstance(err, PermissionError) or getattr(err, "winerror", None) == 5:
                DENIED.setdefault(label, []).append(str(getattr(err, "filename", "") or err))
        for dirpath, dirnames, filenames in os.walk(root, onerror=note):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "~$", "$"))
                           and not any(x.lower() in d.lower() for x in excludes)]
            for name in filenames:
                if name.startswith(("~$", ".")) or name.lower() in ("thumbs.db", "desktop.ini"):
                    continue
                p = Path(dirpath) / name
                if any(x.lower() in str(p.relative_to(root)).lower() for x in excludes):
                    continue
                try:
                    st = p.stat()
                except PermissionError:
                    DENIED.setdefault(label, []).append(str(p))
                    continue
                except OSError:
                    continue
                yield label, root, p, st.st_size, st.st_mtime


def can_open(path):
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except PermissionError:
        return False
    except OSError:
        return True          # other errors (e.g. locked) are not an access refusal


def inventory(roots, excludes, out_dir, check_access=False):
    """Fast overview. Without --check-access no file is opened; with it, each readable file
    is opened for one byte to see whether this user is allowed to read it."""
    import csv
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"inventory-{datetime.now():%Y-%m-%d_%H-%M-%S}.csv"
    by_share, by_ext, by_year, by_top, sensitive = {}, {}, {}, {}, {}
    total = size_all = readable = readable_mb = 0
    readable_ext = TEXT_EXT | HTML_EXT | {".docx", ".xlsx", ".pptx", ".pdf"}
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["share", "folder", "file", "type", "size_kb", "modified"])
        for label, root, p, size, mtime in walk(roots, excludes):
            rel = p.relative_to(root)
            ext = p.suffix.lower() or "(none)"
            year = datetime.fromtimestamp(mtime).year
            top = f"{label}/{rel.parts[0]}" if len(rel.parts) > 1 else f"{label}/(top)"
            w.writerow([label, str(rel.parent), p.name, ext, round(size / 1024),
                        datetime.fromtimestamp(mtime).strftime("%Y-%m-%d")])
            total += 1; size_all += size
            for d, k in ((by_share, label), (by_ext, ext), (by_year, year), (by_top, top)):
                n, b = d.get(k, (0, 0)); d[k] = (n + 1, b + size)
            if ext in readable_ext and size <= 25 * 1024 * 1024:
                if check_access and not can_open(p):
                    DENIED.setdefault(label, []).append(str(p))
                else:
                    readable += 1; readable_mb += size / 1024 / 1024
            if SENSITIVE.search(str(rel)):
                key = f"{label}/{rel.parent}"
                sensitive[key] = sensitive.get(key, 0) + 1
            if total % 2000 == 0:
                print(f"  {total} files listed ...", flush=True)
    gb = lambda b: f"{b / 1024**3:.1f} GB"
    lines = [f"Inventory of {', '.join(str(r) for r in roots)}",
             f"Files: {total:,}   Total size: {gb(size_all)}",
             f"Readable for text (Office, PDF, text; up to 25 MB)"
             + (", and allowed for this user" if check_access else "") + f": {readable:,} files",
             "", "By share:"]
    lines += [f"  {k:<20} {n:>8,} files  {gb(b):>9}" for k, (n, b) in sorted(by_share.items())]
    lines += ["", "Largest top-level folders (by number of files):"]
    lines += [f"  {k[:50]:<50} {n:>7,}  {gb(b):>8}" for k, (n, b) in
              sorted(by_top.items(), key=lambda x: -x[1][0])[:20]]
    lines += ["", "File types:"]
    lines += [f"  {k:<10} {n:>8,}  {gb(b):>8}" for k, (n, b) in
              sorted(by_ext.items(), key=lambda x: -x[1][0])[:15]]
    lines += ["", "Last modified (year):"]
    lines += [f"  {k}: {n:,}" for k, (n, b) in sorted(by_year.items(), reverse=True)[:12]]
    lines += ["", "Access: folders or files this user may NOT open (left untouched):"
              + ("" if check_access else "  [files not tested; use --check-access]")]
    if DENIED:
        for share, items in DENIED.items():
            lines.append(f"  {share:<20} {len(items):>6,} not accessible, e.g. {items[0][:60]}")
        lines.append("  Reading these needs a read-only access grant from IT.")
    else:
        lines.append("  none: everything listed was accessible")
    lines += ["", "Folders whose names suggest SENSITIVE content (check before extracting):"]
    lines += ([f"  {n:>6,}  {k}" for k, n in sorted(sensitive.items(), key=lambda x: -x[1])[:25]]
              or ["  none found by name"])
    # rough planning numbers (text extraction is fast; embedding is the slow part)
    est_chunks = readable * 12
    lines += ["", "Rough time estimate for making everything searchable:",
              f"  about {est_chunks:,} search chunks at ~0.5 per second = "
              f"~{est_chunks / 0.5 / 3600:.0f} hours of embedding (can run over several nights;",
              "  every rerun continues and only adds new or changed files)."]
    report = "\n".join(lines)
    (out_dir / csv_path.name.replace(".csv", ".txt")).write_text(report, encoding="utf-8")
    print("\n" + report)
    print(f"\nFull list: {csv_path}")


def extract(path):
    """(text, skip_reason)"""
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXT:
            return read_text_file(path), None
        if ext in HTML_EXT:
            raw = re.sub(r"(?is)<(script|style).*?</\1>", "", read_text_file(path))
            return html.unescape(re.sub(r"<[^>]+>", " ", raw)), None
        if ext == ".docx":
            return docx_text(path), None
        if ext == ".xlsx":
            return xlsx_text(path), None
        if ext == ".pptx":
            return pptx_text(path), None
        if ext == ".pdf":
            return pdf_text(path)
        if ext in OLD_EXT:
            return None, f"old format {ext} (save as the newer format to include it)"
        return None, "file type not read"
    except (zipfile.BadZipFile, ET.ParseError):
        return None, "file is damaged or password-protected"
    except PermissionError:
        return None, "no permission to read"
    except OSError as err:
        return None, f"could not read ({err.__class__.__name__})"


def main():
    ap = argparse.ArgumentParser(description="Read documents from shared drives, read-only.")
    ap.add_argument("--path", action="append", default=[],
                    help=r'share or folder; repeat for several, e.g. "\\server\Metier"')
    ap.add_argument("--inventory", action="store_true",
                    help="only list what is there (no file is opened)")
    ap.add_argument("--check-access", action="store_true",
                    help="with --inventory: test which files this user may open (1 byte each)")
    ap.add_argument("--max-files", type=int, default=0, help="limit (0 = all files)")
    ap.add_argument("--max-mb", type=float, default=25, help="skip files larger than this (MB)")
    ap.add_argument("--max-chars", type=int, default=300000,
                    help="text kept per document (default 300,000 characters)")
    ap.add_argument("--days", type=int, default=0, help="only files changed in the last N days")
    ap.add_argument("--batch", type=int, default=300, help="documents per saved batch")
    ap.add_argument("--exclude", action="append", default=[],
                    help="skip paths containing this text (repeatable), e.g. --exclude KYC")
    args = ap.parse_args()

    cfg = load_config()
    paths = args.path or list(cfg.get("sdrive_paths", []))
    if not paths:
        print("Which network locations should be read? Paste one per line, as shown in the")
        print(r"File Explorer address bar (e.g. \\server\Metier). Empty line to finish.")
        while True:
            line = input("  location: ").strip().strip('"')
            if not line:
                break
            paths.append(line)
        if not paths:
            sys.exit("No location given.")
        print('Tip: put them in config/settings.json as "sdrive_paths" to skip this question.')
    roots = [Path(p.strip('"')) for p in paths]
    for r in roots:
        if not r.exists():
            sys.exit(f"Not found or not accessible: {r}\nCheck the network location in File "
                     "Explorer (copy the path from the address bar).")
    excludes = args.exclude + list(cfg.get("sdrive_exclude", []))
    data = cfg["_data_dir"]
    if args.inventory:
        print("Inventory (no file contents are read): " + ", ".join(map(str, roots)))
        return inventory(roots, excludes, data / "inventory", check_access=args.check_access)

    index_path = data / "sdrive-index.json"          # what was read before: path -> size:mtime
    try:
        seen = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    cutoff = datetime.now().timestamp() - args.days * 86400 if args.days else 0
    retry_days = int(cfg.get("sdrive_denied_retry_days", 30))
    print("Reading (read-only): " + ", ".join(map(str, roots)))
    if excludes:
        print("Excluding paths containing: " + ", ".join(excludes))
    print("Listing files on the server (this can take a few minutes for large folders) ...",
          flush=True)
    todo, listed = [], 0
    for label, root, p, size, mtime in walk(roots, excludes):
        listed += 1
        if listed % 5000 == 0:
            print(f"  {listed:,} files listed ...", flush=True)
        if mtime < cutoff:
            continue
        stamp = f"{size}:{int(mtime)}"
        prev = seen.get(str(p), "")
        if prev == stamp:
            continue                                  # unchanged since the last run
        if prev.startswith("denied|"):                # refused before: retry rarely, so the
            _, old_stamp, when = prev.split("|", 2)   # server logs few "access denied" lines
            try:
                age = (datetime.now() - datetime.fromisoformat(when)).days
            except ValueError:
                age = retry_days
            if old_stamp == stamp and age < retry_days:
                continue
        todo.append((mtime, size, label, root, p, stamp))
    todo.sort(key=lambda t: t[0], reverse=True)       # newest first
    if args.max_files:
        todo = todo[:args.max_files]
    print(f"Files listed: {listed:,}. New or changed files to read: {len(todo):,}", flush=True)

    batch, skipped, n_docs, n_batches = [], [], 0, 0

    def save_batch():
        nonlocal batch, n_batches
        if not batch:
            return
        n_batches += 1
        stamp = f"SDRIVE-{datetime.now():%Y-%m-%d_%H-%M-%S}-{n_batches:03d}"
        out = data / "acquisitions" / stamp
        k = 1
        while out.exists():                  # never overwrite an earlier batch
            k += 1
            out = data / "acquisitions" / f"{stamp}-{k}"
        out.mkdir(parents=True)
        payload = {"source": "sdrive", "acquisitionMethod": "File share (read-only)",
                   "scope": ", ".join(map(str, roots)),
                   "acquiredAt": datetime.now(timezone.utc).isoformat(),
                   "messages": [m for m, _ in batch]}
        (out / "acquisition.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
        (out / "manifest.json").write_text(json.dumps(
            {"acquiredAt": payload["acquiredAt"], "documents": len(batch)}, indent=1),
            encoding="utf-8")
        for _, (key, stamp) in batch:
            seen[key] = stamp
        index_path.write_text(json.dumps(seen), encoding="utf-8")   # progress is kept
        batch = []

    try:
        for i, (mtime, size, label, root, p, stamp) in enumerate(todo, 1):
            rel = p.relative_to(root)
            print(f"  [{i:,}/{len(todo):,}] {p.name[:70]} ({size / 1024 / 1024:.1f} MB)", flush=True)
            if size > args.max_mb * 1024 * 1024:
                skipped.append((str(rel), f"larger than {args.max_mb:g} MB"))
                seen[str(p)] = stamp
                continue
            try:
                text, reason = extract(p)
            except Exception as err:                 # never let one file stop the run
                if "cryptography" in str(err) or "AES" in str(err):
                    text, reason = None, "encrypted PDF (encryption library missing on this drive)"
                else:
                    text, reason = None, f"could not read ({err.__class__.__name__})"
            if reason == "no permission to read":
                DENIED.setdefault(label, []).append(str(p))
            if text is None or not text.strip():
                skipped.append((str(rel), reason or "no text"))
                if reason == "no permission to read":   # remembered; retried after retry_days
                    seen[str(p)] = f"denied|{stamp}|{datetime.now().isoformat(timespec='seconds')}"
                else:
                    seen[str(p)] = stamp
                continue
            text = re.sub(r"[ \t]+\n", "\n", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            note = ""
            if len(text) > args.max_chars:
                text, note = text[:args.max_chars], " (long document: first part kept)"
            props = office_properties(p)
            top = rel.parts[0] if len(rel.parts) > 1 else "(top)"
            header = [f"Document: {label}/{rel}"]
            for k, lbl in (("title", "Title"), ("author", "Author"),
                           ("last_modified_by", "Last edited by"), ("created", "Created")):
                if props.get(k):
                    header.append(f"{lbl}: {props[k]}")
            batch.append(({
                "kind": "document",
                "entryId": "doc-" + hashlib.sha1(f"{label}/{rel}".encode("utf-8")).hexdigest()[:24],
                "conversationId": f"folder:{label}/{top}",
                "threadTopic": f"{label}/{top}",
                "subject": p.name + note,
                "sender": props.get("author") or label,
                "senderName": props.get("author") or label,
                "senderEmail": "",
                "to": [], "cc": [],
                "receivedTime": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                "folder": f"{label}/{rel.parent}",
                "sourcePath": str(p),
                "properties": props,
                "body": "\n".join(header) + "\n\n" + text,
                "attachments": [],
            }, (str(p), stamp)))
            n_docs += 1
            if len(batch) >= args.batch:
                save_batch()
            if i % 50 == 0:
                print(f"  -- {i:,}/{len(todo):,} files checked, {n_docs:,} documents read", flush=True)
    finally:                                    # whatever happens, keep what was read
        save_batch()
        index_path.write_text(json.dumps(seen), encoding="utf-8")


    reasons = {}
    for _, r in skipped:
        reasons[r] = reasons.get(r, 0) + 1
    print(f"\n{n_docs:,} documents read in {n_batches} batch(es); {len(skipped):,} skipped.")
    for share, items in DENIED.items():
        print(f"  not accessible for this user in {share}: {len(items):,} folder(s)/file(s) "
              "(needs a read-only grant from IT)")
    for r, n in sorted(reasons.items(), key=lambda x: -x[1])[:8]:
        print(f"  skipped {n:>6,}: {r}")
    print("Next: embed.py (makes them searchable). A rerun only reads new or changed files.")


if __name__ == "__main__":
    main()