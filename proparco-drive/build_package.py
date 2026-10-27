#!/usr/bin/env python3
"""
build_package.py - assemble a portable drive folder that runs on Linux and Windows.

Run on the development machine (Linux), from the proparco-drive folder:
    python3 build_package.py                       # build into ../dist/proparco-drive
    python3 build_package.py --with-data           # also copy the current data folder
    python3 build_package.py --out /media/usb      # build straight onto a drive

What it produces:
    dist/proparco-drive/
        start.sh  start.bat          launchers (menu)
        scripts/                     the pipeline
        config/settings.json         data_dir = ~/proparco-data on the target machine
        bin/llamafile-0.10.6-thin    model engine (Linux/macOS)
        bin/llamafile-0.10.6-thin.exe  same file, Windows name
        models/                      Ministral GGUF (extracted) + bge-m3 GGUF
        python/win/                  portable Python for Windows (+ sqlite-vec)
        python/lib-linux/            sqlite-vec for Linux (uses the system python3)
        SHA256SUMS.txt               checksums of every file
    dist/proparco-data/              (with --with-data) acquisitions + database + logs

Needs internet once, for the Windows Python and the sqlite-vec packages.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent

# Pinned versions (all released well over two weeks ago).
PYTHON_WIN_VERSION = "3.12.10"
PYTHON_WIN_URL = (f"https://www.python.org/ftp/python/{PYTHON_WIN_VERSION}/"
                  f"python-{PYTHON_WIN_VERSION}-embed-amd64.zip")
SQLITE_VEC = "sqlite-vec==0.1.6"
ENGINE = "llamafile-0.10.6-thin"
MINISTRAL_LLAMAFILE = "Ministral-3-3B-Instruct-2512-Q4_K_M.llamafile"
MINISTRAL_GGUF = "Ministral-3-3B-Instruct-2512-Q4_K_M.gguf"
EMBED_GGUF = "bge-m3-q8.gguf"
SCRIPTS = ["engine.py", "embed.py", "generate.py", "verify.py", "obs.py", "search.py",
           "eval.py", "menu.py", "viewer.py", "gmail_acquire.py", "outlook_acquire.ps1"]


def step(msg):
    print(f"\n== {msg}", flush=True)


def copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"   copied {src.name}")


def extract_ministral(llamafile, out_gguf):
    """A .llamafile is a program with a zip archive inside. Take out the main model
    (the largest .gguf) and leave the image projector (mmproj) behind."""
    with zipfile.ZipFile(llamafile) as z:
        ggufs = [i for i in z.infolist() if i.filename.lower().endswith(".gguf")]
        if not ggufs:
            sys.exit(f"No .gguf found inside {llamafile}")
        for i in ggufs:
            print(f"   inside: {i.filename}  ({i.file_size / 1e9:.2f} GB)")
        main = max(ggufs, key=lambda i: i.file_size)
        args = [i for i in z.infolist() if i.filename.endswith(".args")]
        if args:
            print("   built-in default arguments (.args):")
            print("     " + z.read(args[0]).decode("utf-8", "replace").replace("\n", " "))
        out_gguf.parent.mkdir(parents=True, exist_ok=True)
        with z.open(main) as src, open(out_gguf, "wb") as dst:
            shutil.copyfileobj(src, dst, length=16 * 1024 * 1024)
    print(f"   extracted {main.filename} -> {out_gguf.name}")


def windows_python(dest, offline_zip=None):
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = Path(offline_zip) if offline_zip else dest.parent / "python-win.zip"
    if not offline_zip:
        print(f"   downloading {PYTHON_WIN_URL}")
        urllib.request.urlretrieve(PYTHON_WIN_URL, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    if not offline_zip:
        zip_path.unlink()
    # The ._pth file decides Python's search path in the embeddable build.
    pth = next(dest.glob("python3*._pth"))          # e.g. python312._pth
    pth.write_text(f"{pth.stem}.zip\n.\nLib\\site-packages\nimport site\n", encoding="ascii")
    print(f"   portable Python {PYTHON_WIN_VERSION} ready ({pth.name} configured)")


def pip_target(target, platform_tag, python_version=None):
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--target", str(target),
           "--only-binary=:all:", "--platform", platform_tag, SQLITE_VEC]
    if python_version:
        cmd[cmd.index("--platform"):cmd.index("--platform")] = ["--python-version",
                                                                 python_version]
    print("   " + " ".join(cmd[2:]))
    subprocess.run(cmd, check=True)


def write_settings(src_settings, dst_settings):
    cfg = json.loads(src_settings.read_text(encoding="utf-8"))
    cfg["data_dir"] = "~/proparco-data"        # on the target machine's own disk
    cfg["generation"]["binary"] = f"bin/{ENGINE}"
    cfg["generation"]["model"] = f"models/{MINISTRAL_GGUF}"
    cfg["embedding"]["binary"] = f"bin/{ENGINE}"
    cfg["embedding"]["model"] = f"models/{EMBED_GGUF}"
    dst_settings.parent.mkdir(parents=True, exist_ok=True)
    dst_settings.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    print("   settings.json written (data_dir = ~/proparco-data, both models via the engine)")


def checksums(root):
    lines = []
    for f in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "SHA256SUMS.txt"):
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
        lines.append(f"{h.hexdigest()}  {f.relative_to(root).as_posix()}")
    (root / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"   {len(lines)} files listed in SHA256SUMS.txt")


def main():
    ap = argparse.ArgumentParser(description="Build the portable Linux + Windows package.")
    ap.add_argument("--out", default=str(SRC.parent / "dist"),
                    help="output folder (default: ../dist)")
    ap.add_argument("--with-data", action="store_true",
                    help="also copy the current data folder next to the drive folder")
    ap.add_argument("--python-zip", help="use an already downloaded Windows embeddable zip")
    ap.add_argument("--skip-windows", action="store_true", help="Linux-only package")
    args = ap.parse_args()

    out_root = Path(args.out).expanduser().resolve()
    drive = out_root / "proparco-drive"
    default_out = (SRC.parent / "dist").resolve()
    if args.with_data and out_root != default_out:
        sys.exit("--with-data only works with the default output folder (../dist).\n"
                 "It copies client emails, which must never be written to a flash disk.\n"
                 "Build onto the disk without --with-data, and copy the data to the laptop "
                 "separately.")
    if drive.exists():
        sys.exit(f"{drive} already exists. Remove it or choose another --out.")

    step("Checking source files")
    # On Windows the engine is saved as .exe; accept either name.
    engine_src = SRC / "bin" / ENGINE
    if not engine_src.exists() and (SRC / "bin" / f"{ENGINE}.exe").exists():
        engine_src = SRC / "bin" / f"{ENGINE}.exe"
    needed = [engine_src, SRC / "models" / EMBED_GGUF,
              SRC / "config" / "settings.json", SRC / "start.sh", SRC / "start.bat"]
    needed += [SRC / "scripts" / s for s in SCRIPTS]
    ministral_src = SRC / "models" / MINISTRAL_GGUF
    if not ministral_src.exists():
        needed.append(SRC / "models" / MINISTRAL_LLAMAFILE)
    data_src = None
    if args.with_data:
        cfg = json.loads((SRC / "config" / "settings.json").read_text(encoding="utf-8"))
        data_src = Path(cfg["data_dir"]).expanduser()
        if not data_src.is_absolute():
            data_src = SRC / data_src
        needed.append(data_src / "db" / "proparco.db")
    missing = [str(p) for p in needed if not p.exists()]
    if missing:
        sys.exit("Missing:\n  " + "\n  ".join(missing))
    print("   all present")

    step("Scripts, launchers and config")
    for s in SCRIPTS:
        copy(SRC / "scripts" / s, drive / "scripts" / s)
    for f in ("start.sh", "start.bat", "README.md", "SETUP.md"):
        if (SRC / f).exists():
            copy(SRC / f, drive / f)
    (drive / "start.sh").chmod(0o755)
    if (SRC / "config" / "eval_set.json").exists():
        copy(SRC / "config" / "eval_set.json", drive / "config" / "eval_set.json")
    write_settings(SRC / "config" / "settings.json", drive / "config" / "settings.json")

    step("Model engine (one file, two names)")
    copy(engine_src, drive / "bin" / ENGINE)
    (drive / "bin" / ENGINE).chmod(0o755)
    copy(engine_src, drive / "bin" / f"{ENGINE}.exe")

    step("Models")
    if ministral_src.exists():
        copy(ministral_src, drive / "models" / MINISTRAL_GGUF)
    else:
        extract_ministral(SRC / "models" / MINISTRAL_LLAMAFILE, drive / "models" / MINISTRAL_GGUF)
    copy(SRC / "models" / EMBED_GGUF, drive / "models" / EMBED_GGUF)

    step("sqlite-vec for Linux")
    pip_target(drive / "python" / "lib-linux", "manylinux2014_x86_64")

    if not args.skip_windows:
        step("Portable Python for Windows")
        windows_python(drive / "python" / "win", args.python_zip)
        step("sqlite-vec for Windows")
        pip_target(drive / "python" / "win" / "Lib" / "site-packages", "win_amd64",
                   PYTHON_WIN_VERSION.rsplit(".", 1)[0])

    if args.with_data:
        step("Current data (kept OUTSIDE the drive folder)")
        data_dst = out_root / "proparco-data"
        shutil.copytree(data_src, data_dst,
                        ignore=shutil.ignore_patterns("*.db-wal", "*.db-shm", "proparco_v*.db"))
        print(f"   copied {data_src} -> {data_dst}")

    step("Checksums")
    checksums(drive)

    size = sum(p.stat().st_size for p in drive.rglob("*") if p.is_file()) / 1e9
    print(f"\nDone: {drive}  ({size:.1f} GB)")
    print("Linux:   ./start.sh            Windows: start.bat")
    if args.with_data:
        print("Data:    copy proparco-data to the home folder of the target machine "
              "(~/proparco-data or C:\\Users\\<name>\\proparco-data).")


if __name__ == "__main__":
    main()