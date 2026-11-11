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
    python3 scripts/obs.py dashboard         # charts in an offline HTML page
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



# ================================================================ dashboard

DASH_CSS = """
:root { color-scheme: light; --bg:#f4f7f8; --surface:#ffffff; --ink:#123b49; --text:#1d2a30;
  --muted:#5e6d74; --rule:#d5dee1; --ok:#2f7d4f; --warn:#a86b12; --bad:#a3243b;
  --read:#1f6f8b; --write:#7a4fa3; }
@media (prefers-color-scheme: dark) { :root { color-scheme: dark; --bg:#0f171a; --surface:#152126;
  --ink:#9fd3e0; --text:#dde8eb; --muted:#93a4ab; --rule:#2a3a40; --ok:#6cc08e; --warn:#e0a948;
  --bad:#ef7d8f; --read:#6cc3dd; --write:#b997e0; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font: 15px/1.55 Seravek, "Gill Sans Nova", Ubuntu, Calibri, "DejaVu Sans", sans-serif;
  font-variant-numeric: tabular-nums; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 30px; line-height:1.2; color:var(--ink); margin:0 0 6px; font-weight:600; }
.lede { color:var(--muted); margin:0 0 28px; max-width:70ch; }
h2 { font-size: 19px; color:var(--ink); margin:40px 0 4px; font-weight:600; }
.note { color:var(--muted); margin:0 0 14px; font-size:14px; max-width:75ch; }
section { border-top: 1px solid var(--rule); }
.scroll { overflow-x:auto; }
table { border-collapse: collapse; width:100%; min-width:640px; font-size:14px; }
th { text-align:left; font-weight:600; color:var(--muted); padding:6px 10px; border-bottom:1px solid var(--rule); }
td { padding:6px 10px; border-bottom:1px solid var(--rule); vertical-align:top; }
td.n, th.n { text-align:right; }
.ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
.timeline { display:grid; grid-template-columns: 170px 1fr 150px; gap:6px 12px; align-items:center;
  font-size:14px; }
.timeline .bar { height:14px; border-radius:2px; min-width:3px; }
.timeline .meta { color:var(--muted); text-align:right; }
.legend { display:flex; gap:18px; color:var(--muted); font-size:13px; margin:6px 0 14px; flex-wrap:wrap; }
.legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; }
svg text { fill: var(--muted); font-size: 12px; }
.empty { color:var(--muted); font-style: italic; }
@media (max-width: 640px) { .timeline { grid-template-columns: 1fr; }
  .timeline .meta { text-align:left; } h1 { font-size:24px; } }
"""

STATUS_CLASS = {"ok": "ok", "dry_run": "ok", "nothing_to_do": "ok", "partial": "warn",
                "interrupted": "warn", "stopped": "warn"}


def _status_color(status):
    return {"ok": "var(--ok)", "warn": "var(--warn)"}.get(STATUS_CLASS.get(status, "bad"),
                                                         "var(--bad)")


def _speed_svg(calls):
    """Read and write speed of each generation call, oldest to newest."""
    pts = [(i, c.get("read_tok_s"), c.get("write_tok_s")) for i, c in enumerate(calls)]
    vals = [v for _, r, w in pts for v in (r, w) if v]
    if not vals:
        return '<p class="empty">No generation calls with speed data yet.</p>'
    w, h, pad = 960, 220, 40
    top = max(vals) * 1.15
    n = max(len(pts) - 1, 1)
    x = lambda i: pad + (w - 2 * pad) * i / n
    y = lambda v: h - pad + 10 - (h - pad) * v / top
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" '
             f'aria-label="Read and write speed per model call">']
    for frac in (0, 0.5, 1):
        v = top * frac
        parts.append(f'<line x1="{pad}" x2="{w - pad}" y1="{y(v):.1f}" y2="{y(v):.1f}" '
                     f'stroke="var(--rule)"/><text x="4" y="{y(v) + 4:.1f}">{v:.0f}</text>')
    for key, color in ((1, "var(--read)"), (2, "var(--write)")):
        series = [(x(i), y(p[key])) for i, *_ in pts for p in [pts[i]] if p[key]]
        if len(series) > 1:
            parts.append('<polyline fill="none" stroke-width="1.5" stroke="' + color + '" points="'
                         + " ".join(f"{a:.1f},{b:.1f}" for a, b in series) + '"/>')
        for a, b in series:
            parts.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="3" fill="{color}"/>')
    parts.append(f'<text x="4" y="12">tokens/s</text>')
    parts.append(f'<text x="{pad}" y="{h - 4}">oldest call</text>'
                 f'<text x="{w - pad}" y="{h - 4}" text-anchor="end">newest call</text></svg>')
    return "".join(parts)


def dashboard(log_dir, open_browser=False):
    import html
    log_dir = Path(log_dir)
    esc = lambda v: html.escape(str(v if v is not None else ""))
    events = _load_events(log_dir)
    runs = defaultdict(list)
    for e in events:
        runs[e["run_id"]].append(e)

    # ---- per-run summary
    rows = []
    for run_id in sorted(runs):
        ev = runs[run_id]
        end = next((e for e in ev if e["event"] == "run_end"), None)
        calls = [e for e in ev if e["event"] == "model_call"]
        if end:
            status, secs = end["status"], end["seconds"]
        else:
            status = "no end"
            secs = (datetime.fromisoformat(ev[-1]["ts"]) -
                    datetime.fromisoformat(ev[0]["ts"])).total_seconds()
        model_secs = sum(c.get("seconds", 0) for c in calls)
        rows.append({"run": run_id, "script": ev[0]["script"], "start": ev[0]["ts"][:16].replace("T", " "),
                     "status": status, "secs": secs, "calls": len(calls),
                     "tin": sum(c.get("tokens_in", 0) for c in calls),
                     "tout": sum(c.get("tokens_out", 0) for c in calls),
                     "errors": sum(1 for e in ev if e["event"] == "error"),
                     "warnings": sum(1 for e in ev if e["event"] == "warning"),
                     "model_share": min(model_secs / secs, 1.0) if secs else 0})
    recent = rows[-30:]

    # ---- headline
    if rows:
        last = rows[-1]
        head = (f"Last run: {esc(last['script'])}, {esc(last['status'])}, "
                f"{last['secs'] / 60:.1f} min, {last['calls']} model calls, "
                f"{last['tin']:,} tokens read and {last['tout']:,} written.")
    else:
        head = "No runs logged yet. Run a script from the menu and open this page again."
    total_tin = sum(r["tin"] for r in rows)
    total_tout = sum(r["tout"] for r in rows)
    total_err = sum(r["errors"] for r in rows)

    # ---- timeline
    longest = max((r["secs"] for r in recent), default=1) or 1
    tl = []
    for r in recent:
        width = max(0.5, 100 * r["secs"] / longest)
        tl.append(f'<div>{esc(r["start"])} <span class="note">{esc(r["script"])}</span></div>'
                  f'<div><div class="bar" title="{esc(r["status"])}" style="width:{width:.1f}%;'
                  f'background:{_status_color(r["status"])}"></div></div>'
                  f'<div class="meta">{r["secs"] / 60:.1f} min, {r["tin"] + r["tout"]:,} tok</div>')

    # ---- runs table
    trs = "".join(
        f'<tr><td>{esc(r["start"])}</td><td>{esc(r["script"])}</td>'
        f'<td class="{STATUS_CLASS.get(r["status"], "bad")}">{esc(r["status"])}</td>'
        f'<td class="n">{r["secs"] / 60:.1f}</td><td class="n">{r["calls"]}</td>'
        f'<td class="n">{r["tin"]:,}</td><td class="n">{r["tout"]:,}</td>'
        f'<td class="n">{100 * r["model_share"]:.0f}%</td>'
        f'<td class="n {"bad" if r["errors"] else ""}">{r["errors"]}</td>'
        f'<td class="n {"warn" if r["warnings"] else ""}">{r["warnings"]}</td></tr>'
        for r in reversed(recent))

    # ---- speed
    gen_calls = [e for e in events if e["event"] == "model_call" and e.get("server") == "generation"]

    # ---- problems
    problems = [e for e in events if e["event"] in ("error", "warning")][-25:]
    prob_rows = "".join(
        f'<tr><td>{esc(e["ts"][:16].replace("T", " "))}</td><td>{esc(e["script"])}</td>'
        f'<td class="{"bad" if e["event"] == "error" else "warn"}">{esc(e["event"])}</td>'
        f'<td>{esc(e.get("message", ""))}'
        + (f'<br><span class="note">{esc(e.get("error_type", ""))}: {esc(e.get("error", ""))[:200]}</span>'
           if e.get("error") else "")
        + "</td></tr>" for e in reversed(problems))

    # ---- verification
    vf = [e for e in events if e["event"] == "verification_failed"]
    by_check = Counter(c for e in vf for c in e.get("checks", []))
    vmax = max(by_check.values(), default=1)
    vrows = "".join(
        f'<div>{esc(k)}</div><div><div class="bar" style="width:{100 * v / vmax:.0f}%;'
        f'background:var(--warn)"></div></div><div class="meta">{v} email(s)</div>'
        for k, v in by_check.most_common())

    # ---- quality
    eval_path = log_dir / "eval-history.jsonl"
    evals = []
    if eval_path.exists():
        for line in eval_path.read_text(encoding="utf-8").splitlines():
            try:
                evals.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    erows = "".join(
        f'<tr><td>{esc(h["when"][:16].replace("T", " "))}</td><td>{esc(h["prompt_version"])}</td>'
        f'<td class="n">{h["threads"]}</td><td class="n">{100 * h["recall"]:.0f}%</td>'
        f'<td class="n">{h["forbidden"]}</td><td class="n">{h["rule_errors"]}</td>'
        f'<td class="n">{esc(h["checks_passed"])}</td><td class="n">{h["review_flags"]}</td></tr>'
        for h in reversed(evals[-15:]))

    generated = datetime.now().strftime("%d/%m/%Y %H:%M")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pipeline log, {generated}</title><style>{DASH_CSS}</style></head><body><main>
<h1>{head}</h1>
<p class="lede">{len(rows)} runs logged, {total_tin:,} tokens read and {total_tout:,} written in total,
{total_err} errors. Built from the local log files on {generated}. The logs hold timings, counts and IDs
only, never email content.</p>

<section><h2>Recent runs</h2>
<p class="note">Each bar is one run, sized by how long it took.</p>
<div class="legend"><span><i style="background:var(--ok)"></i>completed</span>
<span><i style="background:var(--warn)"></i>partial or interrupted</span>
<span><i style="background:var(--bad)"></i>failed or no end recorded</span></div>
<div class="timeline">{''.join(tl) or '<p class="empty">No runs yet.</p>'}</div></section>

<section><h2>Model speed per call</h2>
<p class="note">Tokens per second for each summary call. <span style="color:var(--read)">Reading</span>
is how fast emails are taken in; <span style="color:var(--write)">writing</span> is how fast the notes
are produced. A drop usually means another program was using the processor.</p>
{_speed_svg(gen_calls[-200:])}</section>

<section><h2>Runs in detail</h2><div class="scroll"><table>
<tr><th>Started (UTC)</th><th>Script</th><th>Result</th><th class="n">Minutes</th><th class="n">Calls</th>
<th class="n">Tokens in</th><th class="n">Tokens out</th><th class="n">Time in model</th>
<th class="n">Errors</th><th class="n">Warnings</th></tr>{trs}</table></div></section>

<section><h2>Problems</h2>
<p class="note">The 25 most recent errors and warnings. Run "Recent errors and warnings" in the menu
for full details.</p>
<div class="scroll">{'<table><tr><th>When (UTC)</th><th>Script</th><th>Type</th><th>Message</th></tr>'
 + prob_rows + '</table>' if prob_rows else '<p class="empty">No errors or warnings logged.</p>'}</div>
</section>

<section><h2>Held for review</h2>
<p class="note">Emails whose generated notes contained a number, name or source reference that could not
be found in the email. They stay out of Notion until someone checks them.</p>
<div class="timeline">{vrows or '<p class="empty">No email has failed verification.</p>'}</div></section>

<section><h2>Quality scores</h2>
<p class="note">Results of the evaluation against facts written down by hand, newest first.</p>
<div class="scroll">{'<table><tr><th>When (UTC)</th><th>Prompt version</th><th class="n">Threads</th>'
 '<th class="n">Fact recall</th><th class="n">Forbidden</th><th class="n">Rule errors</th>'
 '<th class="n">Checks</th><th class="n">Review flags</th></tr>' + erows + '</table>'
 if erows else '<p class="empty">No evaluation run yet. Fill in the gold set and run the quality evaluation.</p>'}</div>
</section></main></body></html>"""
    out = log_dir / "dashboard.html"
    out.write_text(page, encoding="utf-8")
    print(f"Dashboard written: {out}")
    if open_browser:
        import webbrowser
        webbrowser.open(out.as_uri())
    return out



# ------------------------------------------------------------------ live timer
def fmt_secs(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class Heartbeat:
    """Prints "still working" with the elapsed time every `every` seconds while a long step
    runs, so a slow model call never looks frozen.  with Heartbeat("reading"): ..."""
    def __init__(self, label, every=15):
        import threading
        self.label, self.every = label, every
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.started = None

    def _run(self):
        while not self._stop.wait(self.every):
            print(f"      ... {self.label}: {fmt_secs(time.time() - self.started)} so far", flush=True)

    def start(self):
        self.started = time.time()
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        return time.time() - self.started

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from engine import load_config

    ap = argparse.ArgumentParser(description="Show pipeline runs and errors.")
    ap.add_argument("command", choices=["report", "errors", "dashboard"])
    ap.add_argument("--last", type=int, default=10)
    a = ap.parse_args()
    folder = load_config()["_data_dir"] / "logs"
    if a.command == "dashboard":
        dashboard(folder, open_browser=True)
    else:
        (report if a.command == "report" else errors)(folder, a.last)