#!/usr/bin/env python3
"""
gmail_acquire.py - export Gmail messages, threads and attachments into the common
acquisition format (acquisition.json + manifest.json + attachments/), the same format
the Outlook export writes. embed.py and generate.py then work unchanged.

Two sources, standard library only (runs on the portable Python):

  Live mailbox over IMAP:
    python3 scripts/gmail_acquire.py imap --user you@gmail.com --days 30
    python3 scripts/gmail_acquire.py imap --user you@gmail.com --days 90 --query "label:proparco"
      Needs IMAP enabled in Gmail and an app password (Google Account > Security >
      2-Step Verification > App passwords). The password is asked for and never stored.
      The mailbox is opened read-only; messages are not marked as read.

  Google Takeout export, offline:
    python3 scripts/gmail_acquire.py mbox --file "All mail Including Spam and Trash.mbox" --days 30

Threads: Gmail's own thread ID (X-GM-THRID) becomes conversationId, so replies group
exactly as they do in Gmail. Spam and Trash are skipped.
"""
import argparse
import email
import email.policy
import email.utils
import getpass
import hashlib
import html.parser
import imaplib
import json
import mailbox
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import obs  # noqa: E402
from engine import load_config  # noqa: E402

SKIP_LABELS = {"spam", "trash"}


def clean_label(label):
    """Gmail labels arrive as \\Inbox, "\\\\Spam" (IMAP-escaped) or plain names; normalise them."""
    label = label.strip().strip('"').replace("\\\\", "\\")
    return label.lstrip("\\")


def skip(labels):
    return any(clean_label(l).lower() in SKIP_LABELS for l in labels)


# ================================================================ message parsing

class _HTMLText(html.parser.HTMLParser):
    """Plain text from an HTML-only email: keeps line breaks, drops scripts and styles."""
    BREAKS = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in self.BREAKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(markup):
    p = _HTMLText()
    p.feed(markup)
    text = re.sub(r"[ \t\u00a0]+", " ", "".join(p.parts))
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def body_text(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):
        raw = part.get_payload(decode=True) or b""
        content = raw.decode("utf-8", errors="replace")
    return html_to_text(content) if part.get_content_type() == "text/html" else content


def addresses(value):
    out = []
    for name, addr in email.utils.getaddresses([str(value)] if value else []):
        if not addr and not name:
            continue
        out.append(f"{name} <{addr}>" if name and addr else (addr or name))
    return out


def iso_date(value):
    try:
        dt = email.utils.parsedate_to_datetime(str(value))
        return dt.isoformat() if dt else None
    except (TypeError, ValueError):
        return None


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "attachment").strip(" .")
    return name[:120] or "attachment"


def to_record(raw_bytes, thread_id, msg_id, labels, att_dir, counter, max_bytes):
    """One email -> the common message record, saving its attachments."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    message_id = (msg.get("Message-ID") or "").strip()
    entry_id = f"gm-{msg_id}" if msg_id else (
        "gm-" + hashlib.sha1((message_id or raw_bytes[:2000].hex()).encode()).hexdigest()[:24])
    if not thread_id:   # no Gmail thread ID: fall back to the first message of the chain
        refs = (msg.get("References") or "").split()
        thread_id = hashlib.sha1((refs[0] if refs else message_id or entry_id)
                                 .encode()).hexdigest()[:24]
    sender = addresses(msg.get("From"))
    attachments = []
    for part in msg.iter_attachments():
        name = part.get_filename()
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        record = {"filename": name, "sizeBytes": len(payload),
                  "sizeMB": round(len(payload) / 1e6, 3), "acquired": False,
                  "localPath": None, "skipReason": None}
        if len(payload) > max_bytes:
            record["skipReason"] = f"larger than {max_bytes // 1_000_000} MB"
        else:
            counter[0] += 1
            path = att_dir / f"{counter[0]:05d}_{safe_name(name)}"
            path.write_bytes(payload)
            record.update(acquired=True, localPath=str(path))
        attachments.append(record)
    return {
        "source": "gmail",
        "entryId": entry_id,
        "conversationId": f"gm-thread-{thread_id}",
        "messageId": message_id,
        "subject": str(msg.get("Subject") or ""),
        "sender": sender[0] if sender else "",
        "senderName": email.utils.parseaddr(str(msg.get("From") or ""))[0],
        "senderEmail": email.utils.parseaddr(str(msg.get("From") or ""))[1].lower(),
        "to": addresses(msg.get("To")),
        "cc": addresses(msg.get("Cc")),
        "receivedTime": iso_date(msg.get("Date")),
        "folder": ", ".join(labels),
        "body": body_text(msg),
        "attachments": attachments,
    }


# ================================================================ sources

def from_imap(args, since, att_dir, counter, max_bytes, ob):
    # Google shows app passwords in groups of four ("abcd efgh ijkl mnop"); spaces are not part of it.
    password = getpass.getpass(f"App password for {args.user} (not stored): ").replace(" ", "")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(args.user, password)
    try:
        # "All Mail" has a different name per language; find it by its \All flag.
        _, boxes = imap.list()
        all_mail = None
        for line in boxes:
            m = re.match(rb'\((?P<flags>[^)]*)\) "[^"]*" (?P<name>.+)', line)
            if m and b"\\All" in m.group("flags"):
                all_mail = m.group("name").decode()
        if not all_mail:
            raise RuntimeError("Could not find Gmail's All Mail folder.")
        imap.select(all_mail, readonly=True)

        query = f"{args.query} " if args.query else ""
        raw_query = (query + f"newer_than:{args.days}d").replace('"', "")
        _, data = imap.uid("SEARCH", "X-GM-RAW", f'"{raw_query}"')
        uids = data[0].split()
        newest = list(reversed(uids))[:args.max]
        ob.event("plan", source="imap", matched=len(uids), fetching=len(newest))
        print(f"{len(uids)} messages match; fetching {len(newest)} (newest first).")

        for i, uid in enumerate(newest, 1):
            _, resp = imap.uid("FETCH", uid, "(X-GM-THRID X-GM-MSGID X-GM-LABELS BODY.PEEK[])")
            head, raw = next(((h, b) for h, b in (r for r in resp if isinstance(r, tuple))), (b"", b""))
            thrid = re.search(rb"X-GM-THRID (\d+)", head)
            msgid = re.search(rb"X-GM-MSGID (\d+)", head)
            labels_m = re.search(rb"X-GM-LABELS \((.*?)\)", head)
            labels = [clean_label(l) for l in
                      re.findall(r'"[^"]*"|\S+', labels_m.group(1).decode(errors="replace"))
                      ] if labels_m else []
            if skip(labels):
                continue
            yield to_record(raw, thrid.group(1).decode() if thrid else None,
                            msgid.group(1).decode() if msgid else None,
                            labels, att_dir, counter, max_bytes)
            if i % 50 == 0:
                ob.checkpoint("fetched", messages=i, of=len(newest))
                print(f"  {i}/{len(newest)}", flush=True)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def from_mbox(args, since, att_dir, counter, max_bytes, ob):
    box = mailbox.mbox(args.file, create=False)
    ob.event("plan", source="mbox", file=Path(args.file).name)
    taken = 0
    for i, msg in enumerate(box, 1):
        labels = [clean_label(l) for l in str(msg.get("X-Gmail-Labels") or "").split(",") if l.strip()]
        if skip(labels):
            continue
        dt = None
        try:
            dt = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        except (TypeError, ValueError):
            pass
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if since and dt and dt < since:
            continue
        thrid = str(msg.get("X-GM-THRID") or "").strip() or None
        yield to_record(msg.as_bytes(), thrid, None, labels, att_dir, counter, max_bytes)
        taken += 1
        if taken >= args.max:
            break
        if i % 500 == 0:
            ob.checkpoint("scanned", messages=i, taken=taken)
            print(f"  scanned {i}, kept {taken}", flush=True)


# ================================================================ main

def main():
    ap = argparse.ArgumentParser(description="Export Gmail into the common acquisition format.")
    sub = ap.add_subparsers(dest="source", required=True)
    p_imap = sub.add_parser("imap", help="live mailbox (read-only)")
    p_imap.add_argument("--user", required=True, help="Gmail address")
    p_imap.add_argument("--query", default="", help='Gmail search, e.g. "label:proparco"')
    p_mbox = sub.add_parser("mbox", help="Google Takeout .mbox file")
    p_mbox.add_argument("--file", required=True)
    for p in (p_imap, p_mbox):
        p.add_argument("--days", type=int, default=30, help="how far back (default 30)")
        p.add_argument("--max", type=int, default=1000, help="maximum messages (default 1000)")
        p.add_argument("--max-attachment-mb", type=int, default=20)
        p.add_argument("--no-attachments", action="store_true")
        p.add_argument("--out", help="acquisitions folder (default: <data_dir>/acquisitions)")
    args = ap.parse_args()

    cfg = load_config()
    out_root = Path(args.out).expanduser() if args.out else cfg["_data_dir"] / "acquisitions"
    run_id = f"GMAIL-{datetime.now():%Y-%m-%d_%H-%M-%S}"
    run_dir = out_root / run_id
    att_dir = run_dir / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    ob = obs.Observer("gmail", cfg["_data_dir"] / "logs",
                      {k: v for k, v in vars(args).items() if k != "user"})

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    max_bytes = 0 if args.no_attachments else args.max_attachment_mb * 1_000_000
    counter, started = [0], time.time()
    source = from_imap if args.source == "imap" else from_mbox

    messages, seen, skipped = [], set(), 0
    for record in source(args, since, att_dir, counter, max_bytes, ob):
        if record["entryId"] in seen:
            skipped += 1
            continue
        seen.add(record["entryId"])
        messages.append(record)

    acquired_at = datetime.now(timezone.utc).isoformat()
    acquisition = {"source": "gmail", "acquisitionMethod": f"Gmail {args.source.upper()}",
                   "scope": args.query if args.source == "imap" and args.query
                   else f"last {args.days} days",
                   "acquiredAt": acquired_at, "messages": messages}
    threads = len({m["conversationId"] for m in messages})
    n_att = sum(1 for m in messages for a in m["attachments"] if a["acquired"])
    manifest = {"runId": run_id, "source": "gmail", "scenario": "assisted", "trigger": "manual",
                "acquiredAt": acquired_at, "durationSeconds": round(time.time() - started, 1),
                "daysBack": args.days, "messageCount": len(messages), "threadCount": threads,
                "attachmentsSaved": n_att, "skippedItemCount": skipped, "maxMessages": args.max,
                "cappedByMaxMessages": len(messages) >= args.max,
                "attachmentContentIncluded": not args.no_attachments,
                "maxAttachmentSizeMB": 0 if args.no_attachments else args.max_attachment_mb,
                "foldersSearched": ["All Mail" if args.source == "imap" else Path(args.file).name],
                "outputFolder": str(run_dir)}
    (run_dir / "acquisition.json").write_text(json.dumps(acquisition, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    print(f"\n{len(messages)} messages in {threads} threads, {n_att} attachments saved.")
    print(f"Written to {run_dir}")
    print("Next: python3 scripts/embed.py --dry-run")
    ob.finish("ok", messages=len(messages), threads=threads, attachments=n_att)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. acquisition.json was not written for this run.")
        obs.finish_active("interrupted")
    except (imaplib.IMAP4.error, OSError, RuntimeError) as err:
        print(f"\nERROR: {err}")
        obs.finish_active("failed", err)
        sys.exit(1)