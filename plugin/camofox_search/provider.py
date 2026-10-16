"""Camofox-backed web-search provider: search + page extraction through the
self-hosted Camofox anti-detection browser.

Every call runs in its own throwaway Camofox session (unique ``userId``) which
is torn down in a ``finally`` block, so plugin activity never leaks into the
browser sessions Hermes' own ``browser_*`` tools own.

Response envelopes follow the Hermes legacy provider contract:

- ``search``:  ``{"success": True, "data": {"web": [{"title", "url",
  "description", "position"}, ...]}}``
- ``extract``: ``{"success": True, "data": [{"url", "title", "content",
  "raw_content", "metadata", "error"?}, ...]}``
- failure:    ``{"success": False, "error": str}``
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from agent.web_search_provider import WebSearchProvider

from .client import CamofoxClient, CamofoxError
from .engines import UnknownEngineError, get_engine
from .settings import camofox_configured, default_engine

logger = logging.getLogger(__name__)

MAX_LIMIT = 20
MAX_EXTRACT_URLS = 10
MAX_EXTRACT_CHARS = 200_000
_READY_TIMEOUT_SECONDS = 12.0
_READY_POLL_SECONDS = 0.4
_SEARCH_EXTRACT_ATTEMPTS = 3
_EXTRACT_RETRY_DELAY = 0.7

_PAGE_TEXT_TEMPLATE = (
    "JSON.stringify({ok:true,title:document.title,url:location.href,"
    "text:((document.body&&document.body.innerText)||'').slice(0,@MAX@)})"
)


class CamofoxSearchProvider(WebSearchProvider):
    """Web search/extract via the local Camofox browser server."""

    @property
    def name(self) -> str:
        return "camofox-search"

    @property
    def display_name(self) -> str:
        return "Camofox Search"

    def is_available(self) -> bool:
        """Cheap gate (no network): CAMOFOX_URL or CAMOFOX_API_KEY is configured."""
        return camofox_configured()

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def get_setup_schema(self) -> dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "local",
            "tag": "Search and extract through the local Camofox anti-detection browser.",
            "env_vars": [
                {"key": "CAMOFOX_URL", "prompt": "Camofox server URL",
                 "url": "https://github.com/jo-inc/camofox-browser"},
                {"key": "CAMOFOX_API_KEY", "prompt": "Camofox API key",
                 "url": "https://github.com/jo-inc/camofox-browser"},
            ],
        }

    # ---- search ----

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return {"success": False, "error": "Empty search query."}
        try:
            engine = get_engine(default_engine())
        except UnknownEngineError as exc:
            return {"success": False, "error": str(exc)}
        try:
            limit = max(1, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError):
            limit = 5

        client = CamofoxClient()
        user_id = CamofoxClient.new_session_id()
        try:
            tab_id = client.create_tab(user_id, engine.build_url(query, limit))
            _await_ready(client, tab_id, user_id)
            rows: list[dict[str, Any]] = []
            for attempt in range(_SEARCH_EXTRACT_ATTEMPTS):
                payload = client.evaluate(tab_id, user_id, engine.extract_js(limit))
                rows = _result_rows(payload, limit)
                if rows or attempt == _SEARCH_EXTRACT_ATTEMPTS - 1:
                    break
                time.sleep(_EXTRACT_RETRY_DELAY)
            logger.info("camofox-search %r via %s: %d result(s)", query, engine.name, len(rows))
            return {"success": True, "data": {"web": rows}}
        except CamofoxError as exc:
            return {"success": False, "error": str(exc)}
        finally:
            client.close_session(user_id)

    # ---- extract ----

    def extract(self, urls: list[str], **kwargs: Any) -> dict[str, Any]:
        del kwargs  # ignore format/include_raw/max_chars: one text rendering is served
        cleaned = [str(u).strip() for u in (urls or []) if str(u).strip()]
        if not cleaned:
            return {"success": False, "error": "No URLs provided to extract."}
        cleaned = cleaned[:MAX_EXTRACT_URLS]

        client = CamofoxClient()
        entries = [_extract_one(client, url) for url in cleaned]
        ok = [entry for entry in entries if not entry.get("error")]
        if not ok:
            first_error = next((entry.get("error") for entry in entries if entry.get("error")), None)
            return {"success": False, "error": first_error or "All extractions failed."}
        return {"success": True, "data": entries}


def _await_ready(client: CamofoxClient, tab_id: str, user_id: str,
                 *, timeout: float = _READY_TIMEOUT_SECONDS) -> bool:
    """Poll ``document.readyState`` until the page settles or the deadline passes.

    Best effort: returns False on timeout (callers continue and extract
    whatever the DOM holds). ``navigate``/``create_tab`` usually return after
    load, so this loop normally exits on the first probe.
    """
    deadline = time.monotonic() + timeout
    probe_timeout = min(10.0, client.timeout)
    while True:
        state = client.evaluate(tab_id, user_id, "document.readyState", timeout=probe_timeout)
        if isinstance(state, str) and state == "complete":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_READY_POLL_SECONDS)


def _result_rows(payload: Any, limit: int) -> list[dict[str, Any]]:
    """Normalize an extraction payload into the ``web`` envelope rows.

    Deduplicates by URL, drops entries without a URL, and assigns 1-based
    ``position`` values.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        rows.append({
            "title": str(item.get("title") or "").strip(),
            "url": url,
            "description": str(item.get("description") or "").strip(),
            "position": len(rows) + 1,
        })
        if len(rows) >= limit:
            break
    return rows


def _extract_one(client: CamofoxClient, url: str) -> dict[str, Any]:
    """Extract readable text from one URL in its own throwaway session."""
    user_id = CamofoxClient.new_session_id()
    try:
        tab_id = client.create_tab(user_id, url)
        _await_ready(client, tab_id, user_id)
        payload = client.evaluate(
            tab_id, user_id,
            _PAGE_TEXT_TEMPLATE.replace("@MAX@", str(MAX_EXTRACT_CHARS)))
        if not isinstance(payload, dict) or not payload.get("ok"):
            return {"url": url, "error": f"Camofox could not read page content for {url}"}
        text = str(payload.get("text") or "").strip()
        final_url = str(payload.get("url") or url).strip() or url
        return {
            "url": url,
            "title": str(payload.get("title") or "").strip(),
            "content": text,
            "raw_content": text,
            "metadata": {"engine": "camofox", "final_url": final_url},
        }
    except CamofoxError as exc:
        return {"url": url, "error": str(exc)}
    finally:
        client.close_session(user_id)
