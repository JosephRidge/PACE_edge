#!/usr/bin/env python3
"""
verify_models.py - check that the AI model files are intact (not damaged by an interrupted copy).

    start.bat verify_models.py

Compares each model's SHA-256 fingerprint with the known-good value. A file can have the right
size and still be damaged (e.g. after a flash disk is unplugged while still writing); a damaged
model gives meaningless results without any error, so this check matters.
Known-good values come from settings.json ("model_sha256", written by the build) or the
defaults below. Takes about a minute (it reads ~3 GB).
"""
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import DRIVE_ROOT, load_config  # noqa: E402

KNOWN = {
    "models/bge-m3-q8.gguf":
        "950f4a8e5e19477a6d3c26d2f162233c20002c601f75e4b002e3239997821167",
    "models/Ministral-3-3B-Instruct-2512-Q4_K_M.gguf":
        "9ed150d4367e68df0ac8e1540f6ddc65b42d0ee26378329d1ecbca60f93fc5f8",
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main():
    cfg = load_config()
    expected = dict(KNOWN)
    expected.update(cfg.get("model_sha256", {}))
    used = {cfg[s]["model"] for s in ("generation", "embedding") if cfg.get(s, {}).get("model")}
    problems = 0
    for rel in sorted(used):
        path = DRIVE_ROOT / rel
        if not path.exists():
            print(f"MISSING   {rel}")
            problems += 1
            continue
        want = expected.get(rel)
        t0 = time.time()
        got = sha256(path)
        took = time.time() - t0
        if want is None:
            print(f"UNKNOWN   {rel}  (no reference value; fingerprint {got[:16]}...)")
        elif got == want:
            print(f"OK        {rel}  ({took:.0f} s)")
        else:
            print(f"DAMAGED   {rel}\n          expected {want[:16]}..., found {got[:16]}...")
            problems += 1
    if problems:
        print("\nOne or more model files are damaged or missing: recopy them from a good build "
              "(or rebuild the package) before using the assistant.")
        sys.exit(1)
    print("\nAll model files are intact.")


if __name__ == "__main__":
    main()