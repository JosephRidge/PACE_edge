"""
notion_client.py - the shared connection to Notion (used by notion_sync, notion_match, notion_push).

Key: NOTION_API_KEY in a .env file (stick root, drive folder or data folder) or the environment.
Proxy: NOTION_PROXY in .env, "notion": {"proxy": ...} in settings.json, or HTTPS_PROXY.
API version 2025-09-03: databases are "data sources"; queries use /data_sources/{id}/query.
"""
import json
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine import secret  # noqa: E402

API = "https://api.notion.com/v1"
VERSION = "2025-09-03"
MIN_INTERVAL = 0.35          # Notion allows about 3 requests per second on average

# Proparco's databases (data source IDs) and the properties the assistant uses.
# Any of these can be overridden in settings.json under "notion".
DEFAULTS = {
    "projects": "22555b11-383c-8157-9661-000b46cd7962",        # ALL Projects
    "counterparties": "22c55b11-383c-8028-b1fb-000bfb493df7",  # ALL  Counterparties
    "contacts": "22455b11-383c-8093-abf4-000b667ff21b",        # ALL Contacts
    "project_fields": {
        "title": "Deal Name",
        "latest": "Latest deal description",
        "history": "History of discussions",
        "next": "Next steps",
        "status": "Status",
        "team": "Team",
        "countries": "Deal Countries",
        "project_id": "Project ID",
        "counterparty_links": ["Prospect/Tier/Target/Client",
                               "Lead investor/Lead arranger/Sponsor/Main Financer",
                               "Other lenders/investors", "Origination channels"],
    },
    "counterparty_fields": {
        "title": "Entity's Name", "projects": "Related projects",
        "last_interaction": "Last interaction", "geography": "Geography", "type": "Type",
    },
    "contact_fields": {
        "last": "Last Name", "first": "First Name", "email": "Email",
        "organisation": "Organisation Name", "job": "Job Title",
    },
}


def notion_settings(cfg):
    merged = json.loads(json.dumps(DEFAULTS))
    for key, value in cfg.get("notion", {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


class NotionError(RuntimeError):
    pass


class Notion:
    def __init__(self, cfg):
        ncfg = cfg.get("notion", {})
        self.base = ncfg.get("api_base", API)
        token = secret("NOTION_API_KEY", cfg, aliases=("NOTION_TOKEN",))
        if not token:
            raise NotionError("No Notion key found. Put NOTION_API_KEY=ntn_... in the .env file "
                              "(stick root or next to start.bat).")
        if not token.startswith(("ntn_", "secret_")):
            raise NotionError("The Notion key does not look right (it should start with ntn_).")
        self.token = token
        proxy = (ncfg.get("proxy") or secret("NOTION_PROXY", cfg)
                 or secret("HTTPS_PROXY", cfg, aliases=("https_proxy",)))
        handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})] if proxy else []
        self.opener = urllib.request.build_opener(*handlers)
        self.calls = 0
        self._last = 0.0

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(6):
            wait = MIN_INTERVAL - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            req = urllib.request.Request(self.base + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}", "Notion-Version": VERSION,
                "Content-Type": "application/json"})
            try:
                with self.opener.open(req, timeout=60) as r:
                    self.calls += 1
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                detail = err.read().decode("utf-8", "replace")[:300]
                if err.code == 429 or err.code >= 500:        # rate limited or Notion hiccup
                    time.sleep(float(err.headers.get("Retry-After", 2 ** attempt)))
                    continue
                if err.code == 401:
                    raise NotionError("Notion refused the key (401). Check NOTION_API_KEY.")
                if err.code == 404:
                    raise NotionError(f"Not found in Notion ({path}). Is the database connected "
                                      "to the integration (... -> Connections)?")
                raise NotionError(f"Notion {method} {path} -> HTTP {err.code}: {detail}")
            except urllib.error.URLError as err:
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise NotionError(f"Cannot reach Notion ({err.reason}). Check the proxy.")
        raise NotionError("Notion kept refusing (rate limit or errors); try again later.")

    def query_all(self, data_source_id, progress=None):
        """Every page of a data source (database), following pagination."""
        pages, cursor = [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            res = self.call("POST", f"/data_sources/{data_source_id}/query", body)
            pages += res.get("results", [])
            if progress:
                progress(len(pages))
            if not res.get("has_more"):
                return pages
            cursor = res.get("next_cursor")


# ---- reading property values -------------------------------------------------------------

def prop(page, name):
    return (page.get("properties") or {}).get(name) or {}


def text_of(page, name):
    p = prop(page, name)
    kind = p.get("type")
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in p.get(kind) or []).strip()
    if kind == "email":
        return (p.get("email") or "").strip()
    if kind in ("select", "status"):
        return ((p.get(kind) or {}).get("name") or "").strip()
    if kind == "date":
        return ((p.get("date") or {}).get("start") or "")
    return ""


def list_of(page, name):
    p = prop(page, name)
    if p.get("type") == "multi_select":
        return [o.get("name", "") for o in p.get("multi_select") or []]
    return []


def relation_ids(page, name):
    p = prop(page, name)
    return [r["id"] for r in p.get("relation") or [] if r.get("id")] if p.get("type") == "relation" else []


def normalise(text):
    """Lower case, accents removed, punctuation to spaces: for name matching."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())