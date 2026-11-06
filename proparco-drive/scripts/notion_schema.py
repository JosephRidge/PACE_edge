#!/usr/bin/env python3
"""
notion_schema.py - show the structure of the Notion databases the integration can access.

    Put the key in the .env file next to start.bat:   NOTION_API_KEY=ntn_...
    (optional, same file)                            NOTION_PROXY=http://172.18.150.11:8080
    start.bat notion_schema.py

Prints each database's name, its properties (name and type), choice options and the links
(relations) between databases. It reads NO page content, so the output is safe to share.
Goes through the proxy in settings.json ("notion": {"proxy": ...}) or HTTPS_PROXY.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import load_config, secret  # noqa: E402

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"


def notion_token(cfg):
    token = secret("NOTION_API_KEY", cfg, aliases=("NOTION_TOKEN",))
    if not token:
        f = cfg["_data_dir"] / "secrets" / "notion_token.txt"
        if f.exists():
            token = f.read_text(encoding="utf-8").strip()
    if not token:
        sys.exit("No Notion key found. Put NOTION_API_KEY=ntn_... in the .env file next to "
                 "start.bat (or set $env:NOTION_API_KEY in PowerShell).")
    if not token.startswith(("ntn_", "secret_")):
        sys.exit("The Notion key does not look right (it should start with ntn_). Check .env.")
    return token


class Notion:
    def __init__(self, cfg):
        ncfg = cfg.get("notion", {})
        self.base = ncfg.get("api_base", API)
        self.token = notion_token(cfg)
        proxy = (ncfg.get("proxy") or secret("NOTION_PROXY", cfg)
                 or secret("HTTPS_PROXY", cfg, aliases=("https_proxy",)))
        handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})] if proxy else []
        self.opener = urllib.request.build_opener(*handlers)

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(5):
            req = urllib.request.Request(self.base + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Notion-Version": VERSION,
                "Content-Type": "application/json"})
            try:
                with self.opener.open(req, timeout=60) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                detail = err.read().decode("utf-8", "replace")[:300]
                if err.code == 429:                          # rate limited: wait as told
                    time.sleep(float(err.headers.get("Retry-After", "1")))
                    continue
                if err.code == 401:
                    sys.exit("Notion refused the key (401). Check NOTION_TOKEN.")
                raise RuntimeError(f"Notion {method} {path} -> HTTP {err.code}: {detail}")
            except urllib.error.URLError as err:
                raise RuntimeError(f"Cannot reach Notion ({err.reason}). Check the proxy setting.")
        raise RuntimeError("Notion kept rate-limiting; try again in a minute.")


def plain(rich):
    return "".join(t.get("plain_text", "") for t in rich or []) or "(untitled)"


def describe(prop, names):
    kind = prop.get("type", "?")
    extra = ""
    if kind in ("select", "multi_select", "status"):
        opts = [o.get("name", "") for o in prop.get(kind, {}).get("options", [])]
        extra = f"  options: {', '.join(opts[:15])}{' ...' if len(opts) > 15 else ''}" if opts else ""
    elif kind == "relation":
        rel = prop.get("relation", {})
        target = rel.get("data_source_id") or rel.get("database_id") or ""
        extra = f"  -> links to: {names.get(target, target[:8] + '...')}"
    elif kind == "rollup":
        extra = f"  (rollup of {prop.get('rollup', {}).get('relation_property_name', '?')})"
    return kind, extra


def main():
    cfg = load_config()
    api = Notion(cfg)
    found, cursor = [], None
    while True:
        body = {"filter": {"property": "object", "value": "data_source"}, "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = api.call("POST", "/search", body)
        found += [r for r in res.get("results", []) if r.get("object") in ("data_source", "database")]
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    if not found:
        sys.exit("The integration sees no databases. Connect it to them in Notion: "
                 "open the database -> ... -> Connections -> add the integration.")

    sources = [api.call("GET", f"/data_sources/{r['id']}") if r.get("object") == "data_source"
               else r for r in found]
    names = {}
    for s in sources:
        names[s["id"]] = plain(s.get("title"))
        for key in ("database_id",):
            if s.get("parent", {}).get(key):
                names[s["parent"][key]] = plain(s.get("title"))

    print(f"Notion databases visible to the integration: {len(sources)}\n")
    for s in sources:
        print(f"=== {plain(s.get('title'))}   (id {s['id']})")
        props = s.get("properties", {})
        ordered = sorted(props.items(), key=lambda kv: (kv[1].get("type") != "title", kv[0].lower()))
        for name, prop in ordered:
            kind, extra = describe(prop, names)
            print(f"   {name:<40} {kind}{extra}")
        print()
    print("No page content was read. This output is safe to share.")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as err:
        sys.exit(f"ERROR: {err}")