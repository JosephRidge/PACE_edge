#!/usr/bin/env python3
"""
bench_threads.py - measure the AI model's real speed on this laptop at several thread counts,
and recommend the fastest setting for "threads" in config/settings.json.

    sh start.sh bench_threads.py              (Linux)     start.bat bench_threads.py  (Windows)
    add --apply to save the best value in settings.json, --counts 4 6 8 to choose what to try

Each test loads the writing model with N threads, reads a fixed ~700-token prompt and writes
~80 tokens, then reports reading and writing speed (tokens per second). About 1-2 minutes per
thread count. Nothing else is changed unless --apply is given.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import obs  # noqa: E402
from engine import Engines, load_config, resolve_threads  # noqa: E402

PROMPT = ("Summarise the state of this investment deal in two sentences, then list the next steps.\n\n" +
          "Email 1 (04/11/2026, Beatrice Mwiriki, KCB): Please find attached the draft Information "
          "Memorandum for KCB's upcoming sustainable bond issuance, covering the USD tranche, the "
          "expected size, tenor, pricing and structure. We would welcome Proparco's comments by Friday "
          "and propose a call next week to discuss participation and the sustainability framework. "
          "The checklist and the draft term sheet will follow, together with the pricing supplement. " * 6)


def physical_cores():
    """Physical cores (Linux: /proc/cpuinfo; elsewhere: half the logical count)."""
    try:
        cores = set()
        phys = core = None
        for line in open("/proc/cpuinfo", encoding="utf-8"):
            if line.startswith("physical id"):
                phys = line.split(":")[1].strip()
            elif line.startswith("core id"):
                core = line.split(":")[1].strip()
                cores.add((phys, core))
        if cores:
            return len(cores)
    except OSError:
        pass
    return max(1, (os.cpu_count() or 2) // 2)


def run(cfg, n):
    cfg = dict(cfg)
    cfg["threads"] = n
    t0 = time.time()
    with Engines(cfg, only=["generation"]) as eng:
        load = time.time() - t0
        best = None
        for _ in range(2):                       # second run: steady state (first warms caches)
            _, t = eng.chat([{"role": "user", "content": PROMPT}], max_tokens=80, temperature=0,
                            return_timings=True, cache_prompt=False)
            best = t
    return load, best.get("prompt_per_second", 0), best.get("predicted_per_second", 0)


def main():
    ap = argparse.ArgumentParser(description="Find the fastest thread count for the AI model.")
    ap.add_argument("--counts", type=int, nargs="*", help="thread counts to try")
    ap.add_argument("--apply", action="store_true", help="save the best value in config/settings.json")
    args = ap.parse_args()
    cfg = load_config()
    logical, phys = os.cpu_count() or 4, physical_cores()
    current = resolve_threads(cfg.get("threads", "auto"))
    counts = args.counts or sorted({max(1, phys // 2), phys, min(logical, phys + 2), current})
    print(f"Processor: {logical} logical CPUs, {phys} physical cores. Current setting: "
          f"{cfg.get('threads', 'auto')} (= {current} threads).")
    print(f"Trying: {', '.join(map(str, counts))} threads (about 1-2 minutes each).\n")

    results = []
    for n in counts:
        print(f"--- {n} threads ...", flush=True)
        try:
            with obs.Heartbeat(f"{n} threads: loading and measuring"):
                load, read, write = run(cfg, n)
        except Exception as err:
            print(f"    failed: {str(err)[:160]}")
            continue
        results.append((n, read, write))
        print(f"    model loaded in {load:.0f}s | reading {read:.1f} tok/s | writing {write:.1f} tok/s", flush=True)

    if not results:
        sys.exit("No measurement succeeded.")
    # A typical summary call reads ~1,000 tokens and writes ~150: weigh both accordingly.
    def seconds(r):
        return 1000 / max(r[1], 0.1) + 150 / max(r[2], 0.1)
    best = min(results, key=seconds)
    print("\nThreads | reading tok/s | writing tok/s | est. seconds per typical call")
    for r in results:
        mark = "  <- fastest" if r is best else ""
        print(f"{r[0]:>7} | {r[1]:>13.1f} | {r[2]:>13.1f} | {seconds(r):>8.0f}{mark}")
    print(f"\nRecommended: \"threads\": {best[0]}")
    if args.apply:
        p = Path(__file__).resolve().parent.parent / "config" / "settings.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        data["threads"] = best[0]
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Saved in {p}.")
    else:
        print("Run again with --apply to save it, or edit config/settings.json.")


if __name__ == "__main__":
    main()