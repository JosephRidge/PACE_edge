#!/usr/bin/env python3
"""
embcheck.py - check that the search (embedding) model returns valid vectors on this machine.

    start.bat embcheck.py

Prints the vector size, how many values are zero or invalid, and a few sample values.
A healthy vector has 1024 values, almost none zero, and small numbers like 0.0213, -0.0457.
"""
import json
import math
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import Engines, load_config  # noqa: E402

TEXTS = ["Proparco en Afrique de l'Ouest",
         "Senior loan of EUR 20 million for a solar plant in Kenya"]


def main():
    cfg = load_config()
    with Engines(cfg, only=["embedding"]) as eng:
        # 1. through the normal path (invalid values are replaced by 0 there)
        for text in TEXTS:
            v = eng.embed([text])[0]
            zeros = sum(1 for x in v if x == 0.0)
            norm = math.sqrt(sum(x * x for x in v))
            print(f"'{text[:40]}'")
            print(f"   dims: {len(v)} | zeros: {zeros} | length: {norm:.3f} | "
                  f"first: {[round(x, 4) for x in v[:5]]}")
        # 2. the raw server answer, before any cleaning (shows nulls/NaN if present)
        try:
            raw = eng.emb.post("/v1/embeddings", {"input": [TEXTS[0]]}, timeout=120)
            values = raw["data"][0]["embedding"]
            if values and isinstance(values[0], list):
                values = values[0]
            bad = sum(1 for x in values if not isinstance(x, (int, float))
                      or (isinstance(x, float) and not math.isfinite(x)))
            print(f"\nRaw server answer: {len(values)} values, {bad} invalid (null or NaN)")
        except Exception as err:
            print(f"\nRaw check not possible ({err.__class__.__name__}: {err})")
    print("\nHealthy: zeros near 0, length about 1.0, invalid 0.")
    print("Broken:  zeros near 1024, or many invalid values.")


if __name__ == "__main__":
    main()