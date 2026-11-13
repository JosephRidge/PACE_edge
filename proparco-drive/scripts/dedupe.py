#!/usr/bin/env python3
"""
dedupe.py - find duplicate emails in a mailbox's EXPORTED copies, review them, remove them.

    start.bat dedupe.py                 (scan, then open the review page in the browser)
    start.bat dedupe.py --scan-only     (scan and print the report; nothing opened or changed)

Works on the data folder (PROPARCO_DATA_DIR, e.g. proparco-data\\mailboxes\\<user>):
its exported emails (acquisitions\\*\\acquisition.json) and its index (db\\proparco.db).
OUTLOOK IS NEVER TOUCHED: nothing is deleted or changed in anyone's mailbox.

Three kinds of duplicate, each group labelled with the strongest link found:
  1. same email      - same Internet Message-ID (e.g. the Inbox copy and the archive copy)
  2. identical       - same sender, subject, minute and text (exports without a Message-ID)
  3. near-duplicate  - the same OWN text (>= 90% alike) under a different header: a forwarded
                       copy, a re-send, the same newsletter twice. Only each email's own words
                       are compared (quoted history removed), so a short reply is never mistaken
                       for a copy of the email it quotes; and the numbers (dates, amounts,
                       references) must be the same, so a recurring report or notice with the
                       same wording but another date or amount is never a duplicate.

One email per group is proposed to keep (most attachments saved, then not in Deleted Items or
Junk, then has a Message-ID, then earliest); the review page can change that choice.

With --attachments, every saved attachment is fingerprinted (SHA-256 of its content; cached, so a
second run does not re-read the files), which adds:
  4. same attachments - the same files (by content, not name), same sender, same subject or
                       within 7 days, and little or no own text: "Please find attached" twice
  - an attachment report: unique files, repeated copies and the space they take, inline images
    (signature logos) counted apart, and the documents that circulate most.
Attachments are found where the export saved them, or beside their export on this machine, or
under PROPARCO_ATTACH_ROOT (the original mailbox folder, when reviewing a copy without them).

Removal is reversible:
  - index: the removed emails' rows are deleted, after a backup of the database
  - exported copies: nothing is deleted; removed emails move to removed-duplicates.json beside
    their export and their attachments to dedupe\\quarantine\\, so Restore puts everything back
    (then run embed.py to index restored emails again).
"""
import argparse
import collections
import csv
import hashlib
import io
import zipfile
import html
import json
import re
import shutil
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import load_config  # noqa: E402

NEAR_THRESHOLD = 0.90          # own-text similarity for a near-duplicate
MIN_OWN_WORDS = 25             # shorter own texts are too short to judge as near-duplicates
MAX_BLOCK = 300                # largest group of same-subject emails compared pairwise
ATT_DAYS = 7                   # "same attachments": same subject, or sent within this many days
BIN_FOLDERS = re.compile(r"deleted|supprim|junk|ind[eé]sirable|spam|corbeille|trash", re.I)
SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|tr|aw|wg|rv|sv|antw)\s*(\[\d+\])?\s*:\s*)+", re.I)

try:
    from embed import _quote_start               # the indexer's own detector of quoted history
except Exception:                                # pragma: no cover - fallback if embed changes
    def _quote_start(lines):
        return None


def own_text(body):
    """Only the email's OWN words: everything before the quoted history. Unlike the indexer's
    cleaning (which, for summaries, falls back to the quoted content when the own text is
    trivial), there is no fallback: a reply's own words are what make it different."""
    lines = (body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    q = _quote_start(lines)
    return "\n".join(lines[:q] if q is not None else lines).strip()


# ================================================================ reading the exports

def norm_subject(s):
    return " ".join(SUBJECT_PREFIX.sub("", s or "").lower().split())


def norm_text(s):
    return " ".join(re.findall(r"\w+", (s or "").lower()))


def load_records(data):
    """Every exported email as a record; key = run folder + position (verified before changes)."""
    recs = []
    for acq in sorted((data / "acquisitions").glob("*/acquisition.json")):
        try:
            msgs = json.loads(acq.read_text(encoding="utf-8-sig")).get("messages") or []
        except Exception as err:
            print(f"  skipped unreadable export {acq.parent.name}: {err}")
            continue
        for i, m in enumerate(msgs if isinstance(msgs, list) else [msgs]):
            if (m.get("kind") or "email") != "email":
                continue
            body = m.get("body") or ""
            own = own_text(body)
            atts = [a for a in (m.get("attachments") or []) if isinstance(a, dict)]
            files = [{"name": a.get("filename") or "", "size": int(a.get("sizeBytes") or 0),
                      "path": a.get("localPath") or "", "inline": bool(a.get("inline")),
                      "saved": bool(a.get("acquired"))} for a in atts]
            recs.append({
                "key": f"{acq.parent.name}#{i}", "run": acq.parent.name, "idx": i,
                "entryId": m.get("entryId") or "", "messageId": (m.get("messageId") or "").strip(),
                "subject": (m.get("subject") or "").strip(), "sender": m.get("sender") or "",
                "senderEmail": (m.get("senderEmail") or "").lower(),
                "received": m.get("receivedTime") or "",
                "folder": m.get("folderPath") or m.get("folder") or "",
                "itemClass": m.get("itemClass") or "",
                "attachments": len(atts), "attachmentsSaved": sum(1 for a in atts if a.get("acquired")),
                "size": len(body), "bodyHash": hashlib.sha1(norm_text(body).encode()).hexdigest(),
                "files": files, "attHashes": frozenset(),
                "senderKey": ((m.get("senderEmail") or "") or (m.get("sender") or "")).lower().strip(),
                "own": own, "ownWords": len(norm_text(own).split()),
            # a re-send or forwarded copy carries the SAME numbers; a recurring report or notice
            # (same wording, another date or amount) does not, and is never a near-duplicate
            "numbers": numbers(own) | numbers(m.get("subject")),
                "preview": " ".join(own.split())[:700] or " ".join(body.split())[:700],
            })
    return recs


# ================================================================ attachments

def attachment_file(data, run, path):
    """Where a saved attachment is on THIS machine: as recorded, beside its export, or under
    PROPARCO_ATTACH_ROOT (the original mailbox folder when reviewing a light copy)."""
    import os
    name = Path((path or "").replace("\\", "/")).name
    if not name:
        return None
    cands = [Path(path)] if path else []
    cands.append(data / "acquisitions" / run / "attachments" / name)
    root = os.environ.get("PROPARCO_ATTACH_ROOT")
    if root:
        cands.append(Path(root) / "acquisitions" / run / "attachments" / name)
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def hash_attachments(data, recs, cache_dir):
    """SHA-256 of every saved attachment's content (cached by export, file name and size)."""
    cache_path = cache_dir / "attachment-hashes.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    todo = [(r, f) for r in recs for f in r["files"] if f["saved"]]
    total_bytes = sum(f["size"] for _, f in todo)
    done_bytes, missing, read_now, t0, last = 0, 0, 0, time.time(), time.time()
    print(f"  fingerprinting {len(todo):,} saved attachments ({total_bytes / 1e9:.1f} GB; cached ones are skipped) ...")
    for r, f in todo:
        key = f"{r['run']}/{Path(f['path'].replace(chr(92), '/')).name}|{f['size']}"
        if key in cache:
            f["hash"] = cache[key]
        else:
            p = attachment_file(data, r["run"], f["path"])
            if p is None:
                missing += 1
                continue
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            f["hash"] = cache[key] = h.hexdigest()
            read_now += 1
        done_bytes += f["size"]
        if time.time() - last > 15:
            last = time.time()
            rate = done_bytes / max(1, time.time() - t0)
            print(f"    {done_bytes / 1e9:.1f} of {total_bytes / 1e9:.1f} GB "
                  f"(~{(total_bytes - done_bytes) / max(rate, 1) / 60:.0f} min left)", flush=True)
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
    cache_path.write_text(json.dumps(cache), encoding="utf-8")
    for r in recs:
        r["attHashes"] = frozenset(f["hash"] for f in r["files"] if f.get("hash") and not f["inline"])
    return {"read": read_now, "missing": missing}


def attachment_report(recs, hstats):
    seen, reclaim, copies, inline_n, inline_bytes = {}, 0, 0, 0, 0
    for r in recs:
        for f in r["files"]:
            h = f.get("hash")
            if not h:
                continue
            if f["inline"]:
                inline_n += 1
                inline_bytes += f["size"]
                continue
            if h in seen:
                copies += 1
                reclaim += f["size"]
                seen[h]["emails"] += 1
            else:
                seen[h] = {"name": f["name"], "size": f["size"], "emails": 1}
    top = sorted((v for v in seen.values() if v["emails"] > 1), key=lambda v: (-v["emails"], -v["size"]))[:20]
    return {"saved": sum(1 for r in recs for f in r["files"] if f["saved"]),
            "fingerprinted": sum(1 for r in recs for f in r["files"] if f.get("hash")),
            "notFound": hstats.get("missing", 0), "unique": len(seen), "repeatedCopies": copies,
            "repeatedBytes": reclaim, "inline": inline_n, "inlineBytes": inline_bytes, "top": top}


def when(r):
    try:
        return datetime.fromisoformat((r["received"] or "")[:19])
    except ValueError:
        return None


# ================================================================ mailbox overview

def overview(recs, now=None):
    """What the export holds: period, emails per year, per mailbox/archive (own vs shared), and how
    much is older than 1, 3 and 5 years. For information only: what to keep or archive is the
    owner's decision, so no action is attached to age."""
    now = now or datetime.now()
    years, stores, ages = collections.Counter(), collections.Counter(), {"1": 0, "3": 0, "5": 0}
    dates = []
    for r in recs:
        stores[(r["folder"] or "?").split("\\")[0] or "?"] += 1
        d = when(r)
        if d:
            dates.append(d)
            years[d.year] += 1
            age = (now - d).days / 365.25
            for k in ages:
                if age > int(k):
                    ages[k] += 1
    dates.sort()
    return {"emails": len(recs), "oldest": dates[0].strftime("%d/%m/%Y") if dates else "",
            "newest": dates[-1].strftime("%d/%m/%Y") if dates else "",
            "years": [[y, years[y]] for y in sorted(years)], "stores": stores.most_common(8),
            "olderThan": ages, "attachments": sum(r["attachments"] for r in recs)}


# ================================================================ finding duplicates

class Groups:
    """Union-find over records, remembering the strongest link (1 strongest) and similarity."""
    def __init__(self, n):
        self.parent = list(range(n))
        self.member_tier = {}      # strongest link that attaches each email to its group
        self.sim = {}

    def find(self, a):
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def link(self, a, b, tier, sim=1.0):
        for x in (a, b):
            self.member_tier[x] = min(self.member_tier.get(x, 9), tier)
        ra, rb = self.find(a), self.find(b)
        s = min(self.sim.get(ra, 1.0), self.sim.get(rb, 1.0), sim)
        if ra != rb:
            self.parent[rb] = ra
        self.sim[ra] = s


NUMBER = re.compile(r"\d+(?:[.,/:-]\d+)*")


def numbers(text):
    """The numbers in a text (dates, amounts, references), as a set."""
    return set(NUMBER.findall(text or ""))


def shingles(text, k=5):
    w = norm_text(text).split()
    return {hash(tuple(w[i:i + k])) for i in range(max(0, len(w) - k + 1))}


def find_duplicates(recs):
    g = Groups(len(recs))
    stats = {"comparisons": 0, "large_blocks_capped": 0}

    by = {}
    for i, r in enumerate(recs):                                   # 1. same Message-ID
        if r["messageId"]:
            by.setdefault(r["messageId"], []).append(i)
    for ids in by.values():
        for j in ids[1:]:
            g.link(ids[0], j, 1)

    by = {}
    for i, r in enumerate(recs):                                   # 2. identical content
        k = (r["sender"].lower(), norm_subject(r["subject"]), r["received"][:16], r["bodyHash"])
        by.setdefault(k, []).append(i)
    for ids in by.values():
        for j in ids[1:]:
            g.link(ids[0], j, 2)

    blocks = {}                                                    # 3. near-duplicates
    for i, r in enumerate(recs):
        if r["ownWords"] < MIN_OWN_WORDS:
            continue
        blocks.setdefault(("s", norm_subject(r["subject"])), []).append(i)
        blocks.setdefault(("t", norm_text(r["own"])[:160]), []).append(i)   # same opening, any subject
    sh = {}
    for ids in blocks.values():
        if len(ids) < 2:
            continue
        if len(ids) > MAX_BLOCK:
            stats["large_blocks_capped"] += 1
            ids = ids[:MAX_BLOCK]
        for a_pos, a in enumerate(ids):
            for b in ids[a_pos + 1:]:
                if g.find(a) == g.find(b):
                    continue
                if recs[a]["numbers"] != recs[b]["numbers"]:
                    stats["different_numbers"] = stats.get("different_numbers", 0) + 1
                    continue
                sa = sh.setdefault(a, shingles(recs[a]["own"]))
                sb = sh.setdefault(b, shingles(recs[b]["own"]))
                if not sa or not sb:
                    continue
                stats["comparisons"] += 1
                sim = len(sa & sb) / len(sa | sb)
                if sim >= NEAR_THRESHOLD:
                    g.link(a, b, 3, sim)

    by = {}                                                        # 4. same attachments
    for i, r in enumerate(recs):
        if r["attHashes"] and r["ownWords"] < MIN_OWN_WORDS:
            by.setdefault(r["attHashes"], []).append(i)
    for ids in by.values():
        for a_pos, a in enumerate(ids[:MAX_BLOCK]):
            for b in ids[a_pos + 1:MAX_BLOCK]:
                ra, rb = recs[a], recs[b]
                if g.find(a) == g.find(b) or ra["senderKey"] != rb["senderKey"]:
                    continue
                if numbers(ra["subject"]) != numbers(rb["subject"]):
                    continue
                ta, tb = when(ra), when(rb)
                close = ta and tb and abs((ta - tb).total_seconds()) <= ATT_DAYS * 86400
                if norm_subject(ra["subject"]) == norm_subject(rb["subject"]) or close:
                    g.link(a, b, 4)

    members = {}
    for i in range(len(recs)):
        members.setdefault(g.find(i), []).append(i)
    groups = []
    for root, ids in members.items():
        if len(ids) < 2:
            continue
        keep = choose_keeper([recs[i] for i in ids])
        # a group is only as certain as its WEAKEST link: one near-duplicate member makes the
        # whole group a near-duplicate group (never auto-selected with the exact ones)
        groups.append({
            "id": hashlib.sha1("|".join(sorted(recs[i]["key"] for i in ids)).encode()).hexdigest()[:12],
            "tier": max(g.member_tier.get(i, 3) for i in ids), "similarity": round(g.sim.get(root, 1.0), 3),
            "members": [dict(public(recs[i]), link=g.member_tier.get(i, 3)) for i in ids], "keep": keep["key"],
            "remove": [recs[i]["key"] for i in ids if recs[i]["key"] != keep["key"]],
            "status": "pending",
        })
    groups.sort(key=lambda x: (x["tier"], -len(x["members"]), x["members"][0]["subject"].lower()))
    return groups, stats


def choose_keeper(rs):
    return sorted(rs, key=lambda r: (
        -r["attachmentsSaved"],                    # most attachments saved: never lose an attachment
        bool(BIN_FOLDERS.search(r["folder"])),     # then: not in Deleted Items / Junk
        not r["messageId"],                        # has a Message-ID
        r["received"] or "9999",                   # earliest
        -r["size"]))[0]


def public(r):
    d = {k: r[k] for k in ("key", "run", "idx", "entryId", "messageId", "subject", "sender", "received",
                           "folder", "itemClass", "attachments", "attachmentsSaved", "size", "preview")}
    d["files"] = [f["name"] for f in r["files"] if not f["inline"]][:6]
    return d


# ================================================================ state, apply, restore

class Store:
    def __init__(self, data):
        self.data = data
        self.dir = data / "dedupe"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.lock = threading.Lock()
        self.state = {"groups": [], "applied": [], "scanned": None, "stats": {}}
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    def save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def scan(self, attachments=False):
        recs = load_records(self.data)
        hstats = hash_attachments(self.data, recs, self.dir) if attachments else None
        groups, stats = find_duplicates(recs)
        self.state["attachments"] = attachment_report(recs, hstats) if attachments else None
        applied_ids = {a["id"] for a in self.state.get("applied", [])}
        self.state["groups"] = [x for x in groups if x["id"] not in applied_ids]
        self.state["stats"] = dict(stats, emails=len(recs))
        self.state["overview"] = overview(recs)
        self.state["scanned"] = datetime.now().isoformat(timespec="seconds")
        self.save()
        return self.state

    # ---------------------------------------------------------------- backup and clean-up list
    def backup(self):
        """Zip the exported emails (acquisition.json, manifests, removal records, review state)
        into dedupe/backups/. Attachments are not copied: removal never deletes them (they move to
        quarantine and come back on Restore)."""
        out = self.dir / "backups"
        out.mkdir(parents=True, exist_ok=True)
        z = out / f"exports-{datetime.now():%Y%m%d-%H%M%S}.zip"
        k = 2
        while z.exists():                                  # never overwrite an earlier backup
            z = out / f"exports-{datetime.now():%Y%m%d-%H%M%S}-{k}.zip"
            k += 1
        n = 0
        with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted((self.data / "acquisitions").glob("*/*.json")):
                zf.write(f, f.relative_to(self.data))
                n += 1
            if self.state_path.exists():
                zf.write(self.state_path, self.state_path.relative_to(self.data))
        self.state["lastBackup"] = {"file": str(z), "at": datetime.now().isoformat(timespec="seconds"),
                                    "files": n, "mb": round(z.stat().st_size / 1e6, 1)}
        self.save()
        return {"ok": True, "message": f"Backup saved: {z} ({n} files, {z.stat().st_size / 1e6:.1f} MB)."}

    def cleanup_csv(self):
        """Every duplicate group still to review, one row per copy, for the owner to find and handle
        in Outlook himself if he chooses. Outlook is never changed by this program."""
        kinds = {1: "same email", 2: "identical", 3: "near-duplicate", 4: "same attachments"}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["group", "kind", "similarity", "proposal", "received", "from", "folder", "subject",
                    "attachments", "why this copy"])
        for g in self.state.get("groups", []):
            for m in g["members"]:
                w.writerow([g["id"], kinds.get(g["tier"], g["tier"]), g.get("similarity", ""),
                            "keep" if m["key"] == g["keep"] else "duplicate",
                            (m.get("received") or "")[:16].replace("T", " "), m.get("sender", ""),
                            m.get("folder", ""), m.get("subject", ""), m.get("attachments", 0),
                            kinds.get(m.get("link", g["tier"]), "")])
        return "\ufeff" + buf.getvalue()            # BOM so Excel opens accents correctly

    # ---------------------------------------------------------------- index (proparco.db)
    def _db(self):
        p = self.data / "db" / "proparco.db"
        if not p.exists():
            return None, None
        backup = p.with_name(f"proparco.db.bak-dedupe-{datetime.now():%Y%m%d-%H%M%S}")
        src = sqlite3.connect(p)
        dst = sqlite3.connect(backup)
        src.backup(dst)                            # consistent copy, even of a WAL database
        dst.close()
        try:
            import sqlite_vec
            src.enable_load_extension(True)
            sqlite_vec.load(src)
            src.enable_load_extension(False)
        except Exception as err:
            src.close()
            raise RuntimeError(f"the vector extension (sqlite-vec) is needed to remove emails from the index: {err}")
        return src, backup

    @staticmethod
    def _drop_from_index(db, entry_id):
        ids = [r[0] for r in db.execute("SELECT id FROM chunks WHERE entry_id = ?", (entry_id,))]
        for cid in ids:
            db.execute("DELETE FROM vec_chunks WHERE rowid = ?", (cid,))
        db.execute("DELETE FROM chunks WHERE entry_id = ?", (entry_id,))
        db.execute("DELETE FROM messages WHERE entry_id = ?", (entry_id,))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'email_results'").fetchone():
            db.execute("DELETE FROM email_results WHERE entry_id = ?", (entry_id,))
        return len(ids)

    # ---------------------------------------------------------------- exported copies
    def _remove_from_export(self, run, items, gid):
        """Move the given messages out of one export's acquisition.json (verified by entryId and
        subject), into removed-duplicates.json; attachments into quarantine."""
        folder = self.data / "acquisitions" / run
        acq = folder / "acquisition.json"
        doc = json.loads(acq.read_text(encoding="utf-8-sig"))
        msgs = doc.get("messages") or []
        bak = folder / "acquisition.json.bak-dedupe"
        if not bak.exists():
            shutil.copy2(acq, bak)
        removed_path = folder / "removed-duplicates.json"
        removed = json.loads(removed_path.read_text(encoding="utf-8")) if removed_path.exists() else []
        done = []
        for it in sorted(items, key=lambda x: -x["idx"]):          # highest position first
            pos = it["idx"]
            if pos >= len(msgs) or (msgs[pos].get("entryId") or "") != it["entryId"] \
                    or (msgs[pos].get("subject") or "").strip() != it["subject"]:
                pos = next((k for k, m in enumerate(msgs) if (m.get("entryId") or "") == it["entryId"]
                            and (m.get("subject") or "").strip() == it["subject"]), None)
            if pos is None:
                continue                                           # already gone: nothing to do
            m = msgs.pop(pos)
            moved = []
            for a in m.get("attachments") or []:
                lp = a.get("localPath") if isinstance(a, dict) else None
                if lp:
                    local = Path(lp) if Path(lp).exists() else folder / "attachments" / Path(lp.replace("\\", "/")).name
                    if local.exists():
                        q = self.dir / "quarantine" / run
                        q.mkdir(parents=True, exist_ok=True)
                        target = q / local.name
                        shutil.move(str(local), str(target))
                        moved.append([str(local), str(target)])
            removed.append({"group": gid, "message": m, "attachmentsMoved": moved,
                            "removedAt": datetime.now().isoformat(timespec="seconds")})
            done.append(it["key"])
        doc["messages"] = msgs
        tmp = acq.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(acq)
        removed_path.write_text(json.dumps(removed, ensure_ascii=False, indent=1), encoding="utf-8")
        return done

    def apply(self, decisions):
        """decisions: {group_id: keep_key}. Removes every other member of those groups."""
        with self.lock:
            groups = {x["id"]: x for x in self.state["groups"]}
            todo = [(groups[g], k) for g, k in decisions.items() if g in groups]
            if not todo:
                return {"ok": False, "message": "Nothing selected."}
            lb = (self.state.get("lastBackup") or {}).get("at")
            auto = ""
            if not lb or (datetime.now() - datetime.fromisoformat(lb)).total_seconds() > 3600:
                auto = self.backup()["message"] + " "
            db, backup = self._db()
            removed_emails = removed_chunks = 0
            try:
                for grp, keep in todo:
                    keep_m = next(m for m in grp["members"] if m["key"] == keep)
                    drop = [m for m in grp["members"] if m["key"] != keep]
                    by_run = {}
                    for m in drop:
                        by_run.setdefault(m["run"], []).append(m)
                    done = []
                    for run, items in by_run.items():
                        done += self._remove_from_export(run, items, grp["id"])
                    if db is not None:
                        for m in drop:
                            # never drop index rows that the kept email shares (same entryId)
                            if m["entryId"] and m["entryId"] != keep_m["entryId"]:
                                removed_chunks += self._drop_from_index(db, m["entryId"])
                    removed_emails += len(done)
                    grp.update(status="applied", keep=keep, removedKeys=done,
                               appliedAt=datetime.now().isoformat(timespec="seconds"))
                    self.state.setdefault("applied", []).append(grp)
                if db is not None:
                    db.commit()
            finally:
                if db is not None:
                    db.close()
            applied = {g["id"] for g, _ in todo}
            self.state["groups"] = [x for x in self.state["groups"] if x["id"] not in applied]
            self.save()
            msg = auto + f"Removed {removed_emails} duplicate email(s) from the exported copies"
            msg += f" and {removed_chunks} indexed piece(s); index backup: {backup.name}." if backup else \
                " (no index found in this data folder)."
            return {"ok": True, "message": msg}

    def restore(self, gid):
        """Put a removed group's emails and attachments back; embed.py then re-indexes them."""
        with self.lock:
            restored = 0
            for acq in (self.data / "acquisitions").glob("*/removed-duplicates.json"):
                removed = json.loads(acq.read_text(encoding="utf-8"))
                keep = [r for r in removed if r["group"] != gid]
                back = [r for r in removed if r["group"] == gid]
                if not back:
                    continue
                main = acq.parent / "acquisition.json"
                doc = json.loads(main.read_text(encoding="utf-8-sig"))
                for r in back:
                    for src, dst in r.get("attachmentsMoved", []):
                        if Path(dst).exists():
                            Path(src).parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(dst, src)
                    doc["messages"].append(r["message"])
                    restored += 1
                tmp = main.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(main)
                acq.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
            self.state["applied"] = [a for a in self.state.get("applied", []) if a["id"] != gid]
            self.save()
            return {"ok": True, "message": f"Restored {restored} email(s). Run embed.py to index them again."}


# ================================================================ review page

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Mailbox review</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e2e1dc;--keep:#2f9e44;--drop:#c92a2a;
--t1:#1971c2;--t2:#6741d9;--t3:#e8590c;--t4:#0c8599;--hi:#fff3bf}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#20201e;--ink:#ecebe6;--mute:#a3a29b;--line:#383732;
--hi:#4a3f12}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--line);padding:14px 20px;z-index:2}
h1{font-size:19px;margin:0 0 4px}.mute{color:var(--mute)}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button,select{font:inherit;padding:6px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.primary{background:var(--drop);color:#fff;border-color:var(--drop)}button:disabled{opacity:.45;cursor:default}
main{padding:16px 20px;max-width:1300px;margin:auto}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:0 0 14px;padding:12px 14px}
.badge{display:inline-block;font-size:12px;padding:2px 8px;border-radius:20px;color:#fff;margin-right:6px}
.t1{background:var(--t1)}.t2{background:var(--t2)}.t3{background:var(--t3)}.t4{background:var(--t4)}
#att,#ov{display:none}#att.open,#ov.open{display:block}.small{font-size:13px}
.yr{display:flex;align-items:end;gap:3px;height:70px;margin:6px 0}.yr div{background:var(--t1);width:22px;border-radius:3px 3px 0 0}
.yl{display:flex;gap:3px;font-size:10px;color:var(--mute)}.yl span{width:22px;text-align:center}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}td,th{text-align:left;padding:5px 6px;border-top:1px solid var(--line);vertical-align:top}
tr.keep td{background:color-mix(in srgb,var(--keep) 10%,transparent)}tr.drop td{color:var(--mute);text-decoration:line-through}
.prev{display:none;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px;margin-top:8px}.prev.open{display:grid}
.prev div{border:1px solid var(--line);border-radius:7px;padding:8px;font-size:13px;max-height:220px;overflow:auto;white-space:pre-wrap}
#msg{margin-top:8px;font-weight:600}.empty{padding:40px;text-align:center}
</style></head><body>
<header><h1>Mailbox review &mdash; exported copies only (Outlook is never changed)</h1>
<div class="mute" id="sum"></div>
<div class="row" style="margin-top:8px">
 <label>Show <select id="flt"><option value="0">all kinds</option><option value="1">1 · same email</option>
 <option value="2">2 · identical</option><option value="3">3 · near-duplicate</option>
 <option value="4">4 · same attachments</option></select></label>
 <button onclick="selKind(1)">Select all "same email"</button><button onclick="selKind(2)">Select all "identical"</button>
 <button onclick="selNone()">Select none</button>
 <button class="primary" id="apply" onclick="apply()">Remove duplicates in selected groups</button>
 <button onclick="rescan()">Scan again</button><button onclick="showApplied()">Removed groups / restore</button>
 <button id="attbtn" onclick="document.getElementById('att').classList.toggle('open')" style="display:none">Attachment report</button>
 <button onclick="document.getElementById('ov').classList.toggle('open')">Mailbox overview</button>
 <button onclick="backup()">Back up the exports</button>
 <a href="api/cleanup.csv"><button>Clean-up list (CSV)</button></a></div>
<div id="ov" class="card small"></div>
<div id="att" class="card small"></div>
<div id="msg"></div></header><main id="list"></main>
<script>
const KIND={1:"same email (Message-ID)",2:"identical content",3:"near-duplicate",4:"same attachments"};
let S={groups:[],applied:[]},sel={},keep={};
const esc=s=>(s??"").toString().replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
async function load(){S=await (await fetch("api/state")).json();S.groups.forEach(g=>{keep[g.id]??=g.keep});render()}
function attPanel(){const a=S.attachments,b=document.getElementById("attbtn"),p=document.getElementById("att");
 if(!a){b.style.display="none";return}b.style.display="";const gb=x=>(x/1e9).toFixed(2)+" GB",mb=x=>(x/1e6).toFixed(1)+" MB";
 p.innerHTML=`<b>Attachments</b>: ${a.saved.toLocaleString()} saved · ${a.unique.toLocaleString()} unique documents · `+
  `${a.repeatedCopies.toLocaleString()} repeated copies (${gb(a.repeatedBytes)}) · ${a.inline.toLocaleString()} inline images such as signature logos (${gb(a.inlineBytes)})`+
  (a.notFound?` · ${a.notFound.toLocaleString()} not found on this machine`:"")+
  `<table><tr><th>Most circulated documents</th><th>In emails</th><th>Size</th></tr>`+
  a.top.map(t=>`<tr><td>${esc(t.name)}</td><td>${t.emails}</td><td>${mb(t.size)}</td></tr>`).join("")+`</table>`}
function ovPanel(){const o=S.overview,p=document.getElementById("ov");if(!o){p.innerHTML="Scan again to see the overview.";return}
 const top=Math.max(1,...o.years.map(y=>y[1])),lb=S.lastBackup;
 p.innerHTML=`<b>${o.emails.toLocaleString()} emails</b> from ${esc(o.oldest)} to ${esc(o.newest)} · ${o.attachments.toLocaleString()} attachments`+
  `<div class="yr">${o.years.map(y=>`<div title="${y[0]}: ${y[1].toLocaleString()}" style="height:${Math.max(2,64*y[1]/top)}px"></div>`).join("")}</div>`+
  `<div class="yl">${o.years.map(y=>`<span>${String(y[0]).slice(2)}</span>`).join("")}</div>`+
  `<div style="margin-top:6px"><b>By mailbox:</b> ${o.stores.map(s=>esc(s[0])+" ("+s[1].toLocaleString()+")").join(" · ")}</div>`+
  `<div><b>Older than 1 year:</b> ${o.olderThan["1"].toLocaleString()} · <b>3 years:</b> ${o.olderThan["3"].toLocaleString()} · <b>5 years:</b> ${o.olderThan["5"].toLocaleString()}`+
  ` <span class="mute">(for information: what to keep or archive is the owner's decision)</span></div>`+
  `<div class="mute">${lb?`Last backup: ${esc(lb.at.replace("T"," "))}, ${lb.files} files, ${lb.mb} MB`:"No backup yet: one is made automatically before the first removal."}</div>`}
async function backup(){msg("Backing up ...");const r=await (await fetch("api/backup",{method:"POST"})).json();msg(r.message,!r.ok);await load()}
function render(){attPanel();ovPanel();const f=+document.getElementById("flt").value,L=document.getElementById("list");
 const gs=S.groups.filter(g=>!f||g.tier===f);const st=S.stats||{};
 const rm=S.groups.reduce((n,g)=>n+g.members.length-1,0);
 document.getElementById("sum").textContent=`${S.groups.length} group(s) to review, ${rm} removable copies, in ${st.emails??"?"} exported emails`+
  ` · scanned ${S.scanned??"-"} · ${S.applied.length} group(s) already removed`;
 L.innerHTML=gs.length?"":'<div class="empty mute">No duplicate groups to review.</div>';
 for(const g of gs){const c=document.createElement("div");c.className="card";
  c.innerHTML=`<div class="row"><label><input type="checkbox" ${sel[g.id]?"checked":""} onchange="sel['${g.id}']=this.checked;upd()"> remove the others</label>
  <span class="badge t${g.tier}">${g.tier} · ${KIND[g.tier]}${g.tier===3?" "+Math.round(g.similarity*100)+"%":""}</span>
  <b>${esc(g.members[0].subject)||"(no subject)"}</b><span class="mute">${g.members.length} copies</span>
  <button onclick="this.closest('.card').querySelector('.prev').classList.toggle('open')">Compare text</button></div>
  <table><tr><th>Keep</th><th>Why</th><th>Received</th><th>From</th><th>Folder</th><th>Type</th><th>Attachments</th><th>Size</th></tr>
  ${g.members.map(m=>`<tr class="${m.key===keep[g.id]?"keep":(sel[g.id]?"drop":"")}"><td><input type="radio" name="k${g.id}" ${m.key===keep[g.id]?"checked":""}
   onchange="keep['${g.id}']='${m.key}';render()"></td><td><span class="badge t${m.link}">${m.link}</span></td><td>${esc((m.received||"").slice(0,16).replace("T"," "))}</td><td>${esc(m.sender)}</td>
   <td>${esc(m.folder)}</td><td>${esc((m.itemClass||"").replace("IPM.",""))}</td><td title="${esc((m.files||[]).join(', '))}">${m.attachmentsSaved}/${m.attachments}${(m.files||[]).length?'<div class="mute small">'+esc((m.files||[]).slice(0,3).join(', '))+'</div>':''}</td><td>${(m.size/1024).toFixed(1)} KB</td></tr>`).join("")}</table>
  <div class="prev">${g.members.map(m=>`<div><b>${esc((m.received||"").slice(0,16).replace("T"," "))} · ${esc(m.folder)}</b>\n\n${esc(m.preview)}</div>`).join("")}</div>`;
  L.appendChild(c)}upd()}
function upd(){const n=Object.values(sel).filter(Boolean).length;const b=document.getElementById("apply");b.disabled=!n;b.textContent=`Remove duplicates in ${n} selected group(s)`}
function selKind(t){S.groups.forEach(g=>{if(g.tier===t)sel[g.id]=true});render()}
function selNone(){sel={};render()}
async function apply(){const ids=Object.keys(sel).filter(k=>sel[k]);if(!ids.length)return;
 const copies=S.groups.filter(g=>ids.includes(g.id)).reduce((n,g)=>n+g.members.length-1,0);
 if(!confirm(`Remove ${copies} duplicate copies in ${ids.length} group(s) from the exported copies and the index?\n\nOutlook is not touched. The index is backed up first, and every removal can be restored.`))return;
 const d={};ids.forEach(i=>d[i]=keep[i]);msg("Removing ...");
 const r=await (await fetch("api/apply",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(d)})).json();
 msg(r.message,!r.ok);sel={};await load()}
async function rescan(){msg("Scanning ...");await fetch("api/scan",{method:"POST"});msg("Scan finished.");await load()}
async function showApplied(){const L=document.getElementById("list");
 L.innerHTML=S.applied.length?"":'<div class="empty mute">Nothing removed yet.</div>';
 for(const g of S.applied){const c=document.createElement("div");c.className="card";
  c.innerHTML=`<div class="row"><span class="badge t${g.tier}">${g.tier} · ${KIND[g.tier]}</span><b>${esc(g.members[0].subject)}</b>
  <span class="mute">${(g.removedKeys||[]).length} removed · ${esc(g.appliedAt||"")}</span>
  <button onclick="restore('${g.id}')">Restore</button></div>`;L.appendChild(c)}}
async function restore(id){if(!confirm("Put this group's removed emails back?"))return;
 const r=await (await fetch("api/restore",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id})})).json();
 msg(r.message,!r.ok);await load();showApplied()}
function msg(t,bad){const m=document.getElementById("msg");m.textContent=t;m.style.color=bad?"var(--drop)":"var(--keep)"}
document.getElementById("flt").onchange=render;load();
</script></body></html>"""


def serve(store, port):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", filename=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html")
            if self.path == "/api/state":
                return self._send(200, json.dumps(store.state, ensure_ascii=False))
            if self.path == "/api/cleanup.csv":
                return self._send(200, store.cleanup_csv(), "text/csv",
                                  filename=f"cleanup-list-{datetime.now():%Y%m%d}.csv")
            self._send(404, "{}")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            try:
                if self.path == "/api/apply":
                    return self._send(200, json.dumps(store.apply(body)))
                if self.path == "/api/backup":
                    return self._send(200, json.dumps(store.backup()))
                if self.path == "/api/restore":
                    return self._send(200, json.dumps(store.restore(body.get("id"))))
                if self.path == "/api/scan":
                    store.scan(attachments=getattr(store, "attachments", False))
                    return self._send(200, json.dumps({"ok": True}))
            except Exception as err:
                return self._send(200, json.dumps({"ok": False, "message": f"Stopped: {err}"}))
            self._send(404, "{}")

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}/"
    print(f"\nReview page: {url}   (Ctrl+C here to stop)")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


# ================================================================ main

def main():
    ap = argparse.ArgumentParser(description="Find, review and remove duplicate emails in the exported copies.")
    ap.add_argument("--scan-only", action="store_true", help="scan and print the report; change nothing")
    ap.add_argument("--attachments", action="store_true",
                    help="also fingerprint attachments (finds 'same attachments' duplicates; reads every file once)")
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    cfg = load_config()
    data = Path(cfg["_data_dir"])
    if not (data / "acquisitions").is_dir():
        sys.exit(f"No exported emails in {data / 'acquisitions'}. Set PROPARCO_DATA_DIR to the mailbox folder.")
    store = Store(data)
    t0 = time.time()
    print(f"Scanning {data / 'acquisitions'} ...")
    st = store.scan(attachments=args.attachments)
    store.attachments = args.attachments
    kinds = {1: 0, 2: 0, 3: 0, 4: 0}
    for x in st["groups"]:
        kinds[x["tier"]] += 1
    copies = sum(len(x["members"]) - 1 for x in st["groups"])
    print(f"  {st['stats']['emails']:,} exported emails scanned in {time.time() - t0:.0f} s")
    print(f"  {len(st['groups'])} duplicate group(s): {kinds[1]} same email, {kinds[2]} identical, "
          f"{kinds[3]} near-duplicate, {kinds[4]} same attachments; {copies} removable copies")
    a = st.get("attachments")
    if a:
        print(f"  attachments: {a['saved']:,} saved, {a['unique']:,} unique documents, {a['repeatedCopies']:,} repeated "
              f"copies ({a['repeatedBytes'] / 1e9:.2f} GB), {a['inline']:,} inline images"
              + (f"; {a['notFound']:,} not found on this machine" if a['notFound'] else ""))
        for t in a["top"][:5]:
            print(f"     in {t['emails']:3} emails: {t['name'][:60]} ({t['size'] / 1e6:.1f} MB)")
    if st["stats"].get("different_numbers"):
        print(f"  {st['stats']['different_numbers']:,} similar pair(s) kept apart: same wording, different "
              f"numbers (recurring reports, notices)")
    if st["stats"].get("large_blocks_capped"):
        print(f"  note: {st['stats']['large_blocks_capped']} very common subject(s) compared on their first "
              f"{MAX_BLOCK} emails only")
    if args.scan_only:
        for x in st["groups"][:15]:
            print(f"   [{x['tier']}] {len(x['members'])}x  {x['members'][0]['subject'][:70]}")
        return
    serve(store, args.port)


if __name__ == "__main__":
    main()
