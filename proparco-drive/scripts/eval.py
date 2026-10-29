#!/usr/bin/env python3
"""
eval.py - score the generated CRM fields against facts you wrote down by hand.

"Write the eval before the thing it tests": config/eval_set.json lists threads and
the facts a correct result must (and must not) contain. This script compares the
stored results with that list and keeps a history, so prompt or model changes can be
compared with numbers instead of impressions.

    python3 scripts/eval.py                # score what is stored now
    python3 scripts/eval.py --generate     # first regenerate the eval threads (--force)
    python3 scripts/eval.py --history      # scores of earlier eval runs

Only IDs and scores are written to the history, never email content.
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import DRIVE_ROOT, load_config  # noqa: E402

HISTORY_LINE = re.compile(r"^\[\d{2}/\d{2}/\d{4}( \(approx\.\))?\] \S")


def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def score_thread(db, case):
    rows = db.execute("""SELECT * FROM email_results WHERE conversation_id LIKE ?
                         ORDER BY seq""", (case["thread"] + "%",)).fetchall()
    if not rows:
        return {"thread": case["thread"], "missing": True}
    last = rows[-1]
    text = norm(f"{last['latest_deal_summary']}\n{last['next_steps']}\n{last['project_history']}")

    must = case.get("must_mention", [])
    hits = [f for f in must if norm(f) in text]
    forbidden = [f for f in case.get("must_not_mention", []) if norm(f) in text]

    history = [l for l in (last["project_history"] or "").split("\n") if l.strip()]
    bad_history = [l for l in history if not HISTORY_LINE.match(l)]
    rule_errors = len(bad_history)
    if len(history) != len(rows):
        rule_errors += 1                     # one history entry per email
    for r in rows:                           # NEW UPDATE! only on update emails
        if ("NEW UPDATE!" in (r["latest_deal_summary"] or "")) != bool(r["deal_update"]):
            rule_errors += 1
    if "**" in text.replace("**new update!**", ""):
        rule_errors += 1                     # stray markdown

    checks = {}
    if "expect_deal_update" in case:
        checks["deal_update"] = any(r["deal_update"] for r in rows) == case["expect_deal_update"]
    if "expect_open_steps" in case:
        open_steps = sum(1 for l in (last["next_steps"] or "").split("\n")
                         if l.startswith("- ") and "No outstanding actions" not in l)
        checks["open_steps"] = open_steps == case["expect_open_steps"]

    flags = sum(sum(1 for f in json.loads(r["verification"] or "[]") if f.get("review"))
                for r in rows if "verification" in r.keys())
    return {"thread": case["thread"], "emails": len(rows),
            "recall": len(hits) / len(must) if must else None,
            "missed": [f for f in must if f not in hits], "forbidden": forbidden,
            "rule_errors": rule_errors, "checks": checks, "review_flags": flags,
            "prompt_version": last["prompt_version"] if "prompt_version" in last.keys() else None,
            "model": last["model"]}


def main():
    ap = argparse.ArgumentParser(description="Score generated fields against the gold set.")
    ap.add_argument("--generate", action="store_true", help="regenerate eval threads first")
    ap.add_argument("--history", action="store_true", help="show earlier eval scores")
    args = ap.parse_args()

    cfg = load_config()
    history_path = cfg["_data_dir"] / "logs" / "eval-history.jsonl"
    if args.history:
        if not history_path.exists():
            sys.exit("No eval history yet.")
        print(f"{'when':20} {'prompt':12} {'threads':>7} {'recall':>7} {'forbid':>7} "
              f"{'rules':>6} {'checks':>7} {'flags':>6}")
        for line in history_path.read_text(encoding="utf-8").splitlines():
            h = json.loads(line)
            print(f"{h['when'][:19]:20} {str(h['prompt_version'])[:12]:12} {h['threads']:>7} "
                  f"{h['recall']:>6.0%} {h['forbidden']:>7} {h['rule_errors']:>6} "
                  f"{h['checks_passed']:>7} {h['review_flags']:>6}")
        return

    gold_path = DRIVE_ROOT / "config" / "eval_set.json"
    if not gold_path.exists():
        sys.exit(f"No gold set at {gold_path}. Copy and fill in eval_set.json first.")
    cases = [c for c in json.loads(gold_path.read_text(encoding="utf-8"))["threads"]
             if not c["thread"].startswith("REPLACE")]
    if not cases:
        sys.exit("The gold set has no real threads yet. Fill in config/eval_set.json.")

    if args.generate:
        for c in cases:
            print(f"Regenerating {c['thread']} ...", flush=True)
            subprocess.run([sys.executable, str(Path(__file__).with_name("generate.py")),
                            "--thread", c["thread"], "--force"], check=False)

    db = sqlite3.connect(cfg["_data_dir"] / "db" / "proparco.db")
    db.row_factory = sqlite3.Row
    results = [score_thread(db, c) for c in cases]

    print(f"\n{'thread':14} {'emails':>6} {'recall':>7} {'forbid':>7} {'rules':>6} "
          f"{'checks':>8} {'flags':>6}  missed")
    for r in results:
        if r.get("missing"):
            print(f"{r['thread'][:14]:14}  no results stored; run generate.py on it first")
            continue
        passed = sum(r["checks"].values())
        print(f"{r['thread'][:14]:14} {r['emails']:>6} "
              f"{(r['recall'] if r['recall'] is not None else 1):>6.0%} "
              f"{len(r['forbidden']):>7} {r['rule_errors']:>6} "
              f"{passed}/{len(r['checks']):<6} {r['review_flags']:>6}  "
              f"{', '.join(r['missed'])[:40]}")

    scored = [r for r in results if not r.get("missing")]
    if not scored:
        return
    recalls = [r["recall"] for r in scored if r["recall"] is not None]
    summary = {
        "when": datetime.now(timezone.utc).isoformat(),
        "prompt_version": scored[-1]["prompt_version"], "model": scored[-1]["model"],
        "threads": len(scored),
        "recall": sum(recalls) / len(recalls) if recalls else 1.0,
        "forbidden": sum(len(r["forbidden"]) for r in scored),
        "rule_errors": sum(r["rule_errors"] for r in scored),
        "checks_passed": f"{sum(sum(r['checks'].values()) for r in scored)}/"
                         f"{sum(len(r['checks']) for r in scored)}",
        "review_flags": sum(r["review_flags"] for r in scored),
        "per_thread": [{k: v for k, v in r.items() if k not in ("missed",)} for r in scored],
    }
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
    print(f"\nOverall: fact recall {summary['recall']:.0%} | forbidden facts "
          f"{summary['forbidden']} | rule errors {summary['rule_errors']} | checks "
          f"{summary['checks_passed']} | verification flags {summary['review_flags']}")
    print(f"Saved to {history_path}  (compare runs with --history)")


if __name__ == "__main__":
    main()