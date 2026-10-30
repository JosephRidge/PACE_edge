#!/usr/bin/env python3
"""
engine.py - start, check and stop the local llamafile servers.

Run from the proparco-drive folder:
    python3 scripts/engine.py test     # start both servers, run checks, stop
    python3 scripts/engine.py serve    # start both and keep running (Ctrl+C to stop)

Use from other scripts:
    from engine import Engines, load_config
    cfg = load_config()
    with Engines(cfg) as eng:
        vectors = eng.embed(["first text", "second text"])
        answer = eng.chat([{"role": "user", "content": "Hello"}])

Standard library only, so it runs on a portable Python with nothing installed.
"""
import json
import math
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DRIVE_ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = platform.system() == "Windows"

# Windows consoles often default to a legacy code page that cannot print "✓" or "—".
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Calls to 127.0.0.1 must never go through a corporate proxy (e.g. HTTPS_PROXY on the AFD laptop).
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- config

def load_config(path=None):
    """Read config/settings.json and resolve the data folder."""
    path = Path(path) if path else DRIVE_ROOT / "config" / "settings.json"
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

    # PROPARCO_DATA_DIR overrides the setting for one run (e.g. a clean test folder).
    data_dir = Path(os.path.expanduser(os.environ.get("PROPARCO_DATA_DIR") or cfg["data_dir"]))
    if not data_dir.is_absolute():
        data_dir = (DRIVE_ROOT / data_dir).resolve()   # "../proparco-data" = next to the package
    cfg["_data_dir"] = data_dir

    # "auto" = a new random key for every run. It is never written to disk.
    if cfg.get("api_key") == "auto":
        cfg["api_key"] = secrets.token_hex(16)
    return cfg


def _program_path(rel):
    """Path to a program on the drive. On Windows, use the .exe copy if it exists."""
    p = DRIVE_ROOT / rel
    if IS_WINDOWS and p.suffix.lower() != ".exe":
        exe = p.with_name(p.name + ".exe")
        if exe.exists():
            return exe
    return p


def _port_in_use(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---------------------------------------------------------------- one server

class Server:
    def __init__(self, name, spec, threads, api_key, log_dir):
        self.name = name
        self.spec = spec
        self.port = spec["port"]
        self.threads = threads
        self.api_key = api_key
        self.log_dir = log_dir
        self.proc = None
        self.log_file = None
        self.log_path = log_dir / f"{name}.log"

    def headers(self):
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def command(self):
        s = self.spec
        program = _program_path(s["binary"])
        if not program.exists():
            raise RuntimeError(f"{self.name}: program not found: {program}")

        # llamafile is an "Actually Portable Executable". Shells know how to start it,
        # but Python's subprocess gets "Exec format error". Starting it through sh works
        # everywhere on Linux/macOS, including USB drives mounted 'noexec'.
        # The path is relative to the drive folder (the working directory), because
        # llamafile mis-reads absolute paths containing '@'. A self-contained .llamafile
        # also re-opens its own file to read the built-in model, so this matters there too.
        if IS_WINDOWS:
            cmd = [str(program)]
        else:
            cmd = ["sh", program.relative_to(DRIVE_ROOT).as_posix()]

        if s.get("model"):
            model = DRIVE_ROOT / s["model"]
            if not model.is_file():
                raise RuntimeError(f"{self.name}: model not found: {model}")
            # Pass the path relative to the drive folder (the server's working directory).
            # llamafile mis-reads absolute paths containing '@', e.g. '@software-engineering'.
            cmd += ["-m", Path(s["model"]).as_posix()]

        cmd += [
            "--server",
            "--host", "127.0.0.1",          # only this computer can connect
            "--port", str(self.port),
            "-t", str(self.threads),
            "-c", str(s["ctx_size"]),
            "-np", "1",                      # one request at a time
            "--gpu", "disable",              # CPU only; keeps llamafile's sandbox on
        ]
        if s.get("embedding"):
            cmd.append("--embedding")
        if self.api_key:
            cmd += ["--api-key", self.api_key]
        cmd += s.get("extra_args", [])
        return cmd

    def start(self):
        if _port_in_use(self.port):
            raise RuntimeError(
                f"{self.name}: port {self.port} is already in use. "
                f"Stop the other program, or change the port in config/settings.json.")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = open(self.log_path, "ab")
        self.log_file.write(f"\n===== start {time.ctime()} =====\n".encode())
        self.log_file.flush()

        extra = {}
        if IS_WINDOWS:
            extra["creationflags"] = subprocess.CREATE_NO_WINDOW  # no extra console window
        cmd = self.command()
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=self.log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(DRIVE_ROOT),
                **extra,
            )
        except OSError as e:
            raise RuntimeError(f"{self.name}: could not start {cmd[0]}: {e}") from None

    def wait_ready(self, timeout):
        """Poll /health until the model is loaded. Fail early if the server crashes."""
        url = f"http://127.0.0.1:{self.port}/health"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"{self.name} stopped while loading (exit code {self.proc.returncode}).\n"
                    f"Last log lines:\n{self.tail()}")
            try:
                req = urllib.request.Request(url, headers=self.headers())
                with _LOCAL.open(req, timeout=2) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                pass  # still loading (503) or not listening yet
            time.sleep(0.5)
        raise RuntimeError(f"{self.name} was not ready after {timeout}s.\nLast log lines:\n{self.tail()}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.log_file:
            self.log_file.close()
            self.log_file = None

    def tail(self, n=15):
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[-n:])
        except OSError:
            return "(no log available)"

    def post(self, path, payload, timeout):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **self.headers()},
        )
        try:
            with _LOCAL.open(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{self.name} returned HTTP {e.code}: {body}") from None


# ---------------------------------------------------------------- both servers

class Engines:
    """Starts both servers on entry and always stops them on exit."""

    def __init__(self, cfg, only=None, obs=None):
        """only: None for both servers, or e.g. ["embedding"] to start just one.
        obs: optional Observer (obs.py) to log server start-up, readiness and shutdown."""
        self.obs = obs
        log_dir = cfg["_data_dir"] / "logs"
        threads = cfg.get("threads", 4)
        key = cfg.get("api_key") or None
        self.cfg = cfg
        self.gen = Server("generation", cfg["generation"], threads, key, log_dir)
        self.emb = Server("embedding", cfg["embedding"], threads, key, log_dir)
        self.servers = [s for s in (self.emb, self.gen) if only is None or s.name in only]

    def __enter__(self):
        try:
            for s in self.servers:
                s.start()
                self._log("server_start", server=s.name, port=s.port,
                          command=" ".join(a for a in s.command() if a != self.gen.api_key))
            for s in self.servers:
                print(f"Loading {s.name} model ...", flush=True)
                t0 = time.time()
                s.wait_ready(self.cfg[s.name].get("startup_timeout", 300))
                self._log("server_ready", server=s.name, load_seconds=round(time.time() - t0, 1))
            print("Ready.", flush=True)
            return self
        except BaseException as exc:
            if self.obs and isinstance(exc, Exception):
                self.obs.error("server start-up failed", exc=exc)
            self.__exit__(None, None, None)
            raise

    def _log(self, kind, **fields):
        if self.obs:
            self.obs.event(kind, **fields)

    def __exit__(self, *exc):
        for s in (self.gen, self.emb):
            if s.proc is None:
                continue
            crashed = s.proc.poll() is not None  # it ended before we asked it to
            s.stop()
            if crashed and self.obs:
                self.obs.error(f"{s.name} server exited unexpectedly", server=s.name,
                               exit_code=s.proc.returncode, log_tail=s.tail(10))
            self._log("server_stop", server=s.name)
        return False

    def embed(self, texts, return_usage=False):
        """Return one vector per text, in the same order (and token usage if asked)."""
        resp = self.emb.post("/v1/embeddings", {"input": list(texts)}, timeout=300)
        data = resp["data"]
        vectors = [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
        want = self.cfg["embedding"]["dimension"]
        if vectors and len(vectors[0]) != want:
            raise RuntimeError(f"Embedding size is {len(vectors[0])}, config expects {want}.")
        return (vectors, resp.get("usage", {})) if return_usage else vectors

    def count_tokens(self, text):
        """Exact token count from the writer model; a cautious estimate if unavailable."""
        try:
            return len(self.gen.post("/tokenize", {"content": text}, timeout=60)["tokens"])
        except Exception:
            return len(text) // 2

    def chat(self, messages, max_tokens=300, temperature=0.2, return_timings=False, **extra):
        """extra: any other server option, e.g. response_format for JSON output."""
        payload = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        payload.update(extra)
        resp = self.gen.post("/v1/chat/completions", payload, timeout=900)
        text = resp["choices"][0]["message"]["content"]
        return (text, resp.get("timings", {})) if return_timings else text


# ---------------------------------------------------------------- command line

def _cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def run_test(cfg):
    started = time.time()
    with Engines(cfg) as eng:
        print(f"Startup time: {time.time() - started:.0f}s\n")

        fr, en, other = eng.embed([
            "Le comité a approuvé le prêt au projet solaire.",
            "The committee approved the loan to the solar project.",
            "My cat likes to sleep on the sofa.",
        ])
        same, diff = _cos(fr, en), _cos(fr, other)
        print(f"Embedding size:          {len(fr)}")
        print(f"FR vs EN (same meaning): {same:.3f}")
        print(f"FR vs unrelated:         {diff:.3f}")
        print("Embedding check:", "PASS" if same > diff + 0.1 else "CHECK MANUALLY", "\n")

        text, t = eng.chat(
            [{"role": "user", "content":
              "In one sentence, in English: Le comité a approuvé le prêt au projet solaire, "
              "sous réserve de la due diligence."}],
            max_tokens=60, return_timings=True)
        print(f"Generation output: {text.strip()}")
        if t:
            print(f"Read speed:  {t.get('prompt_per_second', 0):.1f} tok/s")
            print(f"Write speed: {t.get('predicted_per_second', 0):.1f} tok/s")
    print("\nServers stopped. Logs are in:", cfg["_data_dir"] / "logs")


def run_serve(cfg):
    with Engines(cfg):
        print(f"Generation: http://127.0.0.1:{cfg['generation']['port']}")
        print(f"Embedding:  http://127.0.0.1:{cfg['embedding']['port']}")
        print("Press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping ...")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    actions = {"test": run_test, "serve": run_serve}
    if mode not in actions:
        print("Usage: python3 scripts/engine.py [test|serve]")
        sys.exit(2)
    try:
        actions[mode](load_config())
    except RuntimeError as err:
        print(f"\nERROR: {err}")
        sys.exit(1)