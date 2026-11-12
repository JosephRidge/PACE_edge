#!/usr/bin/env python3
"""
notion_match.py - which Notion projects does each email thread belong to? (dry run)

    start.bat notion_sync.py        (first, to get the local directory)
    start.bat notion_match.py       [--min-score 2] [--limit 50]

For each email thread in proparco.db, finds candidate projects in the Notion directory and
explains why:
  contact     an email address in the thread belongs to a Contact, whose organisation
              is linked to the project                                          (strongest)
  domain      an email domain matches a counterparty's name (e.g. dfcugroup.com -> DFCU)
  deal name   the project's Deal Name appears in the subject or text
  counterparty a counterparty's name appears in the subject or text            (weakest)
Nothing is written to Notion. Results: <data folder>\\notion\\matches.json
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import load_config  # noqa: E402
from notion_client import normalise  # noqa: E402

EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
GENERIC_DOMAINS = {"gmail", "yahoo", "hotmail", "outlook", "live", "icloud", "aol", "proton",
                   "protonmail", "gmx", "msn", "orange", "free", "wanadoo"}
DOMAIN_SUFFIXES = {"com", "co", "org", "net", "int", "gov", "ac", "edu", "info", "biz", "fr", "uk",
                   "ke", "ug", "tz", "rw", "et", "dj", "bi", "mu", "za", "ng", "gh", "eu", "de",
                   "africa", "io", "global", "group", "mail"}
WEAK_WORDS = {"bank", "group", "groupe", "societe", "compagnie", "banque", "fonds", "limited", "ltd", "plc", "sa", "sas", "fund", "funds", "capital",
              "holding", "holdings", "company", "international", "africa", "african", "kenya",
              "east", "the", "and", "de", "of", "partners", "investment", "investments", "finance",
              "financial", "services", "energy", "power", "solar", "trust", "global", "ventures"}
WEIGHTS = {"contact": 3.0, "deal name": 3.0, "domain": 2.0, "counterparty": 1.5}
# Proparco's own group appears in nearly every email: never treated as a counterparty.
OWN_ORGANISATIONS = ("proparco", "afd", "agence francaise de developpement", "fisea", "groupe afd")
# Deals in these states are history: still shown, but ranked below live ones.
INACTIVE = ("closed", "repaid", "exited", "written-off", "dropped", "abandoned")
MAX_FANOUT = 3      # a counterparty mention spreads to at most this many live projects
SECONDARY = 0.3     # weight when the organisation is only a co-lender/investor/channel on the deal
GENERIC_DEAL_WORDS = {"tbd", "debt", "loan", "loans", "senior", "fund", "ii", "iii", "pe", "vc",
                      "the", "and", "de", "of", "project", "new", "2", "3"}
# A real email domain ends in a country code or a known ending. Internal Exchange addresses
# that could not be converted (e.g. "...@groupe.active") are not domains and are ignored.
REAL_TLDS = {"com", "org", "net", "int", "gov", "edu", "info", "biz", "io", "africa", "global",
             "group", "app", "dev", "ngo", "online", "bank", "finance", "capital", "ai", "co"}
# Never enough on their own to name an organisation from a domain.
GENERIC_LABELS = {"groupe", "group", "bank", "banque", "mail", "info", "contact", "admin", "finance",
                  "africa", "global", "capital", "invest", "holding", "holdings"}
# Ordinary words that are also the first word of some counterparty names: never a mention alone.
COMMON_FIRST = {"equity", "family", "national", "standard", "first", "united", "union", "commercial",
                "cooperative", "development", "housing", "access", "prime", "diamond", "guaranty",
                "trust", "central", "general", "industrial", "international", "global", "imperial",
                "victoria", "premier", "credit", "agricultural", "post", "bank", "new", "small",
                "groupe", "group", "societe", "compagnie", "banque", "fonds", "africa", "african"}
ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9&]{2,3}\b")      # 3-4 letters: KCB, NMB, BPR, CRDB, NCBA
NOT_ACRONYMS = {"PLC", "LTD", "LLC", "SAS", "SARL", "BANK", "GROUP", "FUND", "THE", "AND", "TBD"}


def load_threads(db_path, internal):
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    threads = defaultdict(lambda: {"subjects": [], "emails": set(), "text": [], "count": 0,
                                   "last": ""})
    for r in db.execute("""SELECT conversation_id, subject, sender_email, recipients, received_iso,
                                  body_clean FROM messages ORDER BY received_iso"""):
        if (r["conversation_id"] or "").startswith("folder:"):     # server documents, not emails
            continue
        t = threads[r["conversation_id"]]
        t["count"] += 1
        t["subjects"].append(r["subject"] or "")
        t["last"] = max(t["last"], r["received_iso"] or "")
        for addr in [r["sender_email"] or ""] + EMAIL_RE.findall(r["recipients"] or ""):
            addr = addr.lower().strip()
            if addr and addr.split("@")[-1] not in internal:
                t["emails"].add(addr)
        if sum(len(x) for x in t["text"]) < 40000:
            t["text"].append(r["body_clean"] or "")
    db.close()
    return threads


def domain_label(addr):
    parts = [p for p in addr.split("@")[-1].lower().split(".") if p]
    if not parts or not (len(parts[-1]) == 2 and parts[-1].isalpha() or parts[-1] in REAL_TLDS):
        return ""                                    # not a real email domain (e.g. groupe.active)
    keep = [p for p in parts if p not in DOMAIN_SUFFIXES]
    label = max(keep, key=len) if keep else ""
    return "" if label in GENERIC_DOMAINS else label


def strong_tokens(name):
    return [w for w in normalise(name).split() if len(w) >= 3 and w not in WEAK_WORDS]


def domain_matches(label, name):
    """Does an email domain label (e.g. 'familybank') name this counterparty?
    The whole name or its distinctive part must match the START of the label, so that
    'familybank' matches 'Family Bank' but not 'Ruai Family Hospital' or '(AMI)'."""
    if label in GENERIC_LABELS:
        return False
    full = normalise(name).replace(" ", "")
    core = "".join(strong_tokens(name))
    # a label that is only the start of the name must cover most of it: 'nmbbank' ~ 'NMB Bank PLC',
    # but never 'groupe' ~ 'Groupe Duval'
    return any(len(x) >= 4 and (label == x or label.startswith(x)
                                or x.startswith(label) and len(label) >= 5 and len(label) >= 0.7 * len(x))
               for x in (full, core))


def is_own(name, internal):
    n = normalise(name)
    return any(n == o or n.startswith(o + " ") for o in OWN_ORGANISATIONS) or \
        any(n.replace(" ", "") == d.split(".")[0] for d in internal)


def is_inactive(status):
    s = (status or "").lower()
    return any(w in s for w in INACTIVE)


def main():
    ap = argparse.ArgumentParser(description="Match email threads to Notion projects (dry run).")
    ap.add_argument("--min-score", type=float, default=2.0, help="hide weaker candidates")
    ap.add_argument("--limit", type=int, default=0, help="only the N most recent threads")
    args = ap.parse_args()

    cfg = load_config()
    dir_path = cfg["_data_dir"] / "notion" / "directory.json"
    if not dir_path.exists():
        sys.exit("No Notion directory yet. Run notion_sync.py first.")
    d = json.loads(dir_path.read_text(encoding="utf-8"))
    db_path = cfg["_data_dir"] / "db" / "proparco.db"
    if not db_path.exists():
        sys.exit("No email database yet (proparco.db). Capture and embed emails first.")
    internal = {x.lower() for x in cfg.get("internal_domains", [])}

    projects = {p["id"]: p for p in d["projects"]}
    cps = {c["id"]: c for c in d["counterparties"] if not is_own(c["name"], internal)}
    # A counterparty's own "Related projects" are the deals where it is the client; deals that
    # only list it among lenders/investors/channels are weaker evidence.
    projects_of_cp, primary = defaultdict(set), defaultdict(set)
    for c in cps.values():
        own = {pid for pid in c["projects"] if pid in projects}
        projects_of_cp[c["id"]].update(own)
        primary[c["id"]].update(own)
    for p in projects.values():
        for cid in p["counterparties"]:
            projects_of_cp[cid].add(p["id"])
    contact_by_email = {c["email"]: c for c in d["contacts"] if c["email"]}
    deal_names = {p["id"]: normalise(p["name"]) for p in projects.values()
                  if len(normalise(p["name"])) >= 5}
    cp_names = {c["id"]: normalise(c["name"]) for c in cps.values()
                if len(normalise(c["name"])) >= 4 and normalise(c["name"]) not in WEAK_WORDS}

    # Short names used in subjects: acronyms ("KCB" in "KCB Bank") and a distinctive first word
    # ("Centenary" in "Centenary Bank Uganda"); ordinary words such as "Equity" never count alone.
    short_names = defaultdict(set)
    first_count = defaultdict(int)
    for c in cps.values():
        toks = strong_tokens(c["name"])
        if toks:
            first_count[toks[0]] += 1
    for cid, c in cps.items():
        for a in ACRONYM_RE.findall(c["name"] or ""):
            if a not in NOT_ACRONYMS:
                short_names[a.lower()].add(cid)
        toks = strong_tokens(c["name"])
        if toks and len(toks[0]) >= 5 and toks[0] not in COMMON_FIRST and first_count[toks[0]] <= 2:
            short_names[toks[0]].add(cid)

    def deal_words(pid):
        org_words = {w for cid in projects[pid]["counterparties"] if cid in cps
                     for w in normalise(cps[cid]["name"]).split()}
        return [w for w in normalise(projects[pid]["name"]).split()
                if len(w) >= 4 and not w.isdigit() and w not in GENERIC_DEAL_WORDS
                and w not in org_words and w not in WEAK_WORDS]

    threads = load_threads(db_path, internal)
    order = sorted(threads.items(), key=lambda kv: kv[1]["last"], reverse=True)
    if args.limit:
        order = order[:args.limit]

    results, matched = [], 0
    for conv, t in order:
        scores, evidence = defaultdict(float), defaultdict(list)

        credited = set()

        def credit(pid, kind, why, cid=None):
            # one credit per (deal, kind, organisation): three contacts at KCB count once
            key = (pid, kind, cid or why)
            if key in credited:
                if why not in evidence[pid] and len(evidence[pid]) < 6:
                    evidence[pid].append(why)
                return
            credited.add(key)
            weight = WEIGHTS[kind]
            if cid is not None and pid not in primary.get(cid, ()):
                weight *= SECONDARY
            scores[pid] += weight
            evidence[pid].append(why)

        unknown = []
        for addr in sorted(t["emails"]):
            contact = contact_by_email.get(addr)
            if contact:
                for cid in contact["organisations"]:
                    for pid in projects_of_cp.get(cid, ()):
                        credit(pid, "contact", f"contact {contact['name'] or addr} "
                                               f"({cps.get(cid, {}).get('name', '?')})", cid)
                continue
            label = domain_label(addr)
            hit = False
            if label:
                for cid, c in cps.items():
                    if domain_matches(label, c["name"]):
                        for pid in projects_of_cp.get(cid, ()):
                            credit(pid, "domain", f"domain {addr.split('@')[-1]} ~ {c['name']}", cid)
                            hit = True
            if not hit:
                unknown.append(addr)

        text = " " + normalise(" ".join(t["subjects"]) + " " + " ".join(t["text"])) + " "
        subject = " " + normalise(" ".join(t["subjects"])) + " "

        def mention(cid, why):
            # a mention identifies the counterparty, not one deal:
            #  - one live deal: it can only mean that deal (stronger credit)
            #  - a few live deals: credit each
            #  - many live deals: only those whose own words are in the subject ("KCB Bond" ->
            #    KCB Kenya - Sustainable Bond 2026, not KCB - Trade Finance)
            live = [pid for pid in projects_of_cp.get(cid, ()) if not is_inactive(projects[pid]["status"])]
            if len(live) > MAX_FANOUT:
                live = [pid for pid in live if any(f" {w} " in subject for w in deal_words(pid))]
                if len(live) > MAX_FANOUT:
                    return
            for pid in live:
                credit(pid, "counterparty", why, cid)
            if len(live) == 1 and (live[0], "only", cid) not in credited:
                credited.add((live[0], "only", cid))
                scores[live[0]] += 1.0
                evidence[live[0]].append(f"its only live deal with {cps[cid]['name']}")

        for pid, name in deal_names.items():
            if f" {name} " in text:
                credit(pid, "deal name", f"deal name '{projects[pid]['name']}' mentioned")
        for cid, name in cp_names.items():
            if f" {name} " in text:
                mention(cid, f"counterparty '{cps[cid]['name']}' mentioned")
        for short, cids in short_names.items():
            if f" {short} " in subject:
                for cid in cids:
                    mention(cid, f"'{short.upper() if len(short) <= 6 else short}' in the subject ~ {cps[cid]['name']}")
        for pid in list(scores):
            hits = [w for w in deal_words(pid) if f" {w} " in subject or f" {w} " in text]
            if hits:
                scores[pid] += min(2.0, 1.0 * len(hits))
                evidence[pid].append("deal word(s) in the email: " + ", ".join(hits[:3]))
            if is_inactive(projects[pid]["status"]):     # history ranks below live deals
                scores[pid] *= 0.5

        ranked = sorted(((s, pid) for pid, s in scores.items() if s >= args.min_score), reverse=True)[:5]
        if ranked:
            matched += 1
        results.append({
            "conversation_id": conv, "subject": t["subjects"][0] if t["subjects"] else "",
            "emails": t["count"], "last": t["last"][:10], "unknown_addresses": unknown[:10],
            "candidates": [{"project_id": pid, "deal": projects[pid]["name"],
                            "status": projects[pid]["status"], "score": s,
                            "evidence": evidence[pid], "url": projects[pid]["url"]}
                           for s, pid in ranked],
        })

    out = cfg["_data_dir"] / "notion" / "matches.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    for r in results:
        print(f"\n{r['last']} | {r['subject'][:70]} ({r['emails']} email{'s' if r['emails'] > 1 else ''})")
        if not r["candidates"]:
            print("   no project found" + (f"; addresses not in Contacts: {', '.join(r['unknown_addresses'][:4])}"
                                          if r["unknown_addresses"] else ""))
        for c in r["candidates"]:
            print(f"   [{c['score']:.1f}] {c['deal']}  ({c['status'] or 'no status'})")
            for e in c["evidence"][:3]:
                print(f"         - {e}")
    print(f"\n{matched} of {len(results)} threads matched to at least one project "
          f"(score >= {args.min_score}). Saved: {out}")
    print("Dry run: nothing was written to Notion.")


if __name__ == "__main__":
    main()