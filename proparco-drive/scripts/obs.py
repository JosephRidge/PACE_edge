#!/usr/bin/env python3
"""
obs.py - observability for the pipeline: what ran, how long, how many tokens, what failed.

Every script run gets a run_id. Events are appended to
    <data_dir>/logs/events-YYYY-MM.jsonl   (one JSON object per line, for tools)
    <data_dir>/logs/pipeline.log           (one readable line per event, for people)

Logs hold IDs, counts, timings and error messages only. Email subjects and bodies
are never written to the logs.

See recent runs:
    python3 scripts/obs.py report            # last 10 runs
    python3 scripts/obs.py report --last 30
    python3 scripts/obs.py errors            # recent errors with details
"""
import argparse
import json
import platform
import sys
import time
import traceback
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ACTIVE = None  # the Observer of the current run, if any


def short_id(value, n=12):
    """Shorten long Outlook IDs for logs."""
    return (value or "")[:n]


class Observer:
    def __init__(self, script, log_dir, args=None):
        global ACTIVE
        self.script = script
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / f"events-{datetime.now():%Y-%m}.jsonl"
        self.text_path = self.dir / "pipeline.log"
        self.started = time.time()
        self.tokens = Counter()          # tokens_in / tokens_out per server
        self.calls = Counter()           # model calls per server
        self.model_seconds = Counter()   # time spent waiting for each server
        self.stage_seconds = defaultdict(float)
        self.errors = 0
        self.warnings = 0
        self.finished = False
        self._logged = set()   # exceptions already recorded, to avoid double-counting
        ACTIVE = self
        self.event("run_start", args=args or {}, host=platform.node(),
                   os=f"{platform.system()} {platform.release()}",
                   python=platform.python_version())

    # ---------------------------------------------------------------- writing

    def event(self, kind, **fields):
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "run_id": self.run_id, "script": self.script, "event": kind, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        brief = " ".join(f"{k}={v}" for k, v in fields.items()
                         if k not in ("traceback", "args") and not isinstance(v, (dict, list)))
        with open(self.text_path, "a", encoding="utf-8") as f:
            f.write(f"{record['ts']} {self.run_id} {self.script:9} {kind:13} {brief}\n")

    def model_call(self, server, purpose, seconds, timings=None, usage=None, ok=True, **ids):
        """Record one request to a model server, with token counts and speeds."""
        t = timings or {}
        tokens_in = t.get("prompt_n") or (usage or {}).get("prompt_tokens") or 0
        tokens_out = t.get("predicted_n") or 0
        self.calls[server] += 1
        self.tokens[f"{server}_in"] += tokens_in
        self.tokens[f"{server}_out"] += tokens_out
        self.model_seconds[server] += seconds
        self.event("model_call", server=server, purpose=purpose, ok=ok,
                   seconds=round(seconds, 2), tokens_in=tokens_in, tokens_out=tokens_out,
                   cached_tokens=t.get("cache_n"),
                   read_tok_s=round(t.get("prompt_per_second") or 0, 1) or None,
                   write_tok_s=round(t.get("predicted_per_second") or 0, 1) or None,
                   **ids)

    @contextmanager
    def stage(self, name, **ids):
        """Time a block of work. Errors inside it are logged, then re-raised."""
        start = time.time()
        try:
            yield
        except Exception as exc:
            self.error(f"{name} failed", exc=exc, stage=name, **ids)
            raise
        finally:
            self.stage_seconds[name] += time.time() - start

    def checkpoint(self, name, **progress):
        """A progress marker. If a run dies, the report shows the last one reached."""
        self.event("checkpoint", name=name, elapsed=round(time.time() - self.started, 1),
                   **progress)

    def warn(self, message, **fields):
        self.warnings += 1
        self.event("warning", message=message, **fields)

    def error(self, message, exc=None, **fields):
        self.errors += 1
        extra = {}
        if exc is not None:
            self._logged.add(id(exc))
            extra = {"error_type": type(exc).__name__, "error": str(exc)[:2000],
                     "traceback": "".join(traceback.format_exception(
                         type(exc), exc, exc.__traceback__, limit=6))}
        self.event("error", message=message, **extra, **fields)

    def finish(self, status="ok", **summary):
        if self.finished:
            return
        self.finished = True
        seconds = time.time() - self.started
        self.event("run_end", status=status, seconds=round(seconds, 1),
                   errors=self.errors, warnings=self.warnings,
                   model_calls=dict(self.calls), tokens=dict(self.tokens),
                   model_seconds={k: round(v, 1) for k, v in self.model_seconds.items()},
                   stage_seconds={k: round(v, 1) for k, v in self.stage_seconds.items()},
                   **summary)
        print(f"\n[obs] run {self.run_id} {status} in {seconds / 60:.1f} min | "
              f"model calls {sum(self.calls.values())} | "
              f"tokens in {sum(v for k, v in self.tokens.items() if k.endswith('_in')):,} / "
              f"out {sum(v for k, v in self.tokens.items() if k.endswith('_out')):,} | "
              f"errors {self.errors} | warnings {self.warnings}")
        print(f"[obs] log: {self.text_path}")


def finish_active(status, exc=None):
    """Call from a script's __main__ so every run ends with a run_end event."""
    if ACTIVE is None:
        return
    if exc is not None and id(exc) not in ACTIVE._logged:
        ACTIVE.error(f"run stopped: {type(exc).__name__}", exc=exc)
    ACTIVE.finish(status)


# ================================================================ reports

def _load_events(log_dir):
    events = []
    for path in sorted(Path(log_dir).glob("events-*.jsonl")):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def report(log_dir, last):
    runs = defaultdict(list)
    for e in _load_events(log_dir):
        runs[e["run_id"]].append(e)
    if not runs:
        print("No runs logged yet.")
        return
    print(f"{'run':24} {'script':9} {'status':12} {'min':>6} {'calls':>6} "
          f"{'tok in':>9} {'tok out':>8} {'read/s':>7} {'write/s':>8} {'err':>4}")
    for run_id in sorted(runs)[-last:]:
        ev = runs[run_id]
        end = next((e for e in ev if e["event"] == "run_end"), None)
        calls = [e for e in ev if e["event"] == "model_call"]
        reads = [e["read_tok_s"] for e in calls if e.get("read_tok_s")]
        writes = [e["write_tok_s"] for e in calls if e.get("write_tok_s")]
        if end:
            status, minutes = end["status"], end["seconds"] / 60
        else:
            # No run_end: the process was killed or crashed hard.
            last_cp = next((e for e in reversed(ev) if e["event"] == "checkpoint"), None)
            status = "NO END"
            t0 = datetime.fromisoformat(ev[0]["ts"])
            t1 = datetime.fromisoformat(ev[-1]["ts"])
            minutes = (t1 - t0).total_seconds() / 60
            if last_cp:
                status += f" (last: {last_cp.get('name')})"
        errors = sum(1 for e in ev if e["event"] == "error")
        print(f"{run_id:24} {ev[0]['script']:9} {status[:12]:12} {minutes:6.1f} {len(calls):6} "
              f"{sum(e.get('tokens_in', 0) for e in calls):9,} "
              f"{sum(e.get('tokens_out', 0) for e in calls):8,} "
              f"{(sum(reads) / len(reads)) if reads else 0:7.1f} "
              f"{(sum(writes) / len(writes)) if writes else 0:8.1f} {errors:4}")


def errors(log_dir, last):
    errs = [e for e in _load_events(log_dir) if e["event"] in ("error", "warning")][-last:]
    if not errs:
        print("No errors or warnings logged.")
        return
    for e in errs:
        print(f"\n{e['ts']}  {e['run_id']}  {e['script']}  {e['event'].upper()}")
        print(f"  {e.get('message', '')}")
        for k in ("stage", "server", "thread", "email", "error_type", "error"):
            if e.get(k):
                print(f"  {k}: {e[k]}")
        if e.get("traceback"):
            print("  " + e["traceback"].strip().replace("\n", "\n  "))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from engine import load_config

    ap = argparse.ArgumentParser(description="Show pipeline runs and errors.")
    ap.add_argument("command", choices=["report", "errors"])
    ap.add_argument("--last", type=int, default=10)
    a = ap.parse_args()
    folder = load_config()["_data_dir"] / "logs"
    (report if a.command == "report" else errors)(folder, a.last)