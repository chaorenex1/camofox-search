"""Camofox-backed web-search provider: search + page extraction through the
self-hosted Camofox anti-detection browser.

Every call runs in its own throwaway Camofox session (unique ``userId``) which
is torn down in a ``finally`` block, so plugin activity never leaks into the
browser sessions Hermes' own ``browser_*`` tools own.

Engine selection is rule-driven: the rule database (``HERMES_HOME/camofox-search/
camofox-search.db``, see :mod:`rules`) is consulted first — a rule matching the
configured engine id/domain supplies the SERP URL template and extraction JS —
with the built-in engines from :mod:`engines` as the fallback. Content rules
override the generic full-page text extraction in :meth:`extract` for matching
domains. Rules are editable at runtime via the ``camofox-search-manage`` tool.

Response envelopes follow the Hermes legacy provider contract:

- ``search``:  ``{"success": True, "data": {"web": [{"title", "url",
  "description", "position"}, ...]}}``
- ``extract``: ``{"success": True, "data": [{"url", "title", "content",
  "raw_content", "metadata", "error"?, ...}]}``
- failure:    ``{"success": False, "error": str}``
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from agent.web_search_provider import WebSearchProvider

from . import rules as rule_store
from .client import CamofoxClient, CamofoxError
from .engines import (
    UnknownEngineError,
    get_engine,
    render_extract_js,
    render_url_template,
)
from .rules import Rule
from .settings import camofox_configured, default_engine

logger = logging.getLogger(__name__)

MAX_LIMIT = 20
MAX_EXTRACT_URLS = 10
MAX_EXTRACT_CHARS = 200_000
_READY_TIMEOUT_SECONDS = 12.0
_READY_POLL_SECONDS = 0.4
_SEARCH_EXTRACT_ATTEMPTS = 3
_EXTRACT_RETRY_DELAY = 0.7

_RULE_HINT = (" Manage engines/pages with the camofox-search-manage tool "
              "(skill: 'web-camofox-search:camofox-search-rule').")

_PAGE_TEXT_TEMPLATE = (
    "JSON.stringify({ok:true,title:document.title,url:location.href,"
    "text:((document.body&&document.body.innerText)||'').slice(0,@MAX@)})"
)

_DEFAULT_RULE_LABEL = "default"


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
            limit = max(1, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError):
            limit = 5

        try:
            url, expression, label = _search_plan(default_engine(), query, limit)
        except UnknownEngineError as exc:
            return {"success": False, "error": f"{exc}{_RULE_HINT}"}

        client = CamofoxClient()
        user_id = CamofoxClient.new_session_id()
        try:
            tab_id = client.create_tab(user_id, url)
            _await_ready(client, tab_id, user_id)
            rows: list[dict[str, Any]] = []
            for attempt in range(_SEARCH_EXTRACT_ATTEMPTS):
                payload = client.evaluate(tab_id, user_id, expression)
                rows = _result_rows(payload, limit)
                if rows or attempt == _SEARCH_EXTRACT_ATTEMPTS - 1:
                    break
                time.sleep(_EXTRACT_RETRY_DELAY)
            logger.info("camofox-search %r via %s: %d result(s)", query, label, len(rows))
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


def _search_plan(selector: str, query: str, limit: int) -> tuple[str, str, str]:
    """Resolve ``(url, js_expression, label)`` for one search.

    Rule database first — an engine id (``bing``), its domain (``bing.com``) or
    a subdomain (``cn.bing.com``) all resolve to the same rule — then the
    built-in engines. Raises :class:`UnknownEngineError` when neither matches.
    """
    rule = _resolve_search_rule(selector)
    if rule is not None and rule.url_template:
        return (render_url_template(rule.url_template, query, limit),
                render_extract_js(rule.extract_js, limit),
                f"rule:{rule.domain}")
    engine = get_engine(selector)
    return engine.build_url(query, limit), engine.extract_js(limit), f"engine:{engine.name}"


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


# ---- rule lookups (degrade to defaults on any store problem) ----

def _resolve_search_rule(selector: str) -> Optional[Rule]:
    try:
        return rule_store.resolve_search_rule(selector)
    except Exception as exc:  # noqa: BLE001 - rule trouble must not break search
        logger.warning("camofox-search: search-rule lookup for %r failed "
                       "(falling back to built-ins): %s", selector, exc)
        return None


def _resolve_content_rule(url: str) -> Optional[Rule]:
    try:
        return rule_store.resolve_content_rule(url)
    except Exception as exc:  # noqa: BLE001 - rule trouble must not break extract
        logger.warning("camofox-search: content-rule lookup for %r failed "
                       "(falling back to default extraction): %s", url, exc)
        return None


# ---- extract helpers ----

def _extract_one(client: CamofoxClient, url: str) -> dict[str, Any]:
    """Extract readable text from one URL in its own throwaway session.

    A content rule for the URL's host supplies the extraction expression; when
    it yields no usable content the generic full-page extraction runs instead.
    ``metadata.extract_rule`` records which path produced the content.
    """
    user_id = CamofoxClient.new_session_id()
    try:
        tab_id = client.create_tab(user_id, url)
        _await_ready(client, tab_id, user_id)
        rule = _resolve_content_rule(url)
        if rule is not None:
            payload = client.evaluate(tab_id, user_id, rule.extract_js)
            entry = _rule_content_entry(payload, url, rule.domain)
            if entry is not None:
                return entry
            logger.info("camofox-search: content rule %s produced no usable content "
                        "for %s; using default extraction", rule.domain, url)
        payload = client.evaluate(
            tab_id, user_id,
            _PAGE_TEXT_TEMPLATE.replace("@MAX@", str(MAX_EXTRACT_CHARS)))
        if not isinstance(payload, dict) or not payload.get("ok"):
            return {"url": url, "error": f"Camofox could not read page content for {url}"}
        text = str(payload.get("text") or "").strip()
        final_url = str(payload.get("url") or url).strip() or url
        metadata: dict[str, Any] = {
            "engine": "camofox",
            "final_url": final_url,
            "extract_rule": _DEFAULT_RULE_LABEL,
        }
        if rule is not None:
            metadata["extract_rule_attempted"] = rule.domain
        return {
            "url": url,
            "title": str(payload.get("title") or "").strip(),
            "content": text,
            "raw_content": text,
            "metadata": metadata,
        }
    except CamofoxError as exc:
        return {"url": url, "error": str(exc)}
    finally:
        client.close_session(user_id)


def _rule_content_entry(payload: Any, url: str, domain: str) -> Optional[dict[str, Any]]:
    """Parse a content-rule payload into a result entry; None = unusable.

    Contract: a plain string is the content; a JSON object may carry
    ``{ok, title, url, text|content}``. ``ok: false`` or empty content means
    the rule failed for this page (caller falls back to default extraction).
    """
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return None
        title, final_url = "", url
    elif isinstance(payload, dict) and payload.get("ok") is not False:
        text = ""
        for key in ("text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
        if not text:
            return None
        title = str(payload.get("title") or "").strip()
        final_url = str(payload.get("url") or url).strip() or url
    else:
        return None
    text = text[:MAX_EXTRACT_CHARS]
    return {
        "url": url,
        "title": title,
        "content": text,
        "raw_content": text,
        "metadata": {
            "engine": "camofox",
            "final_url": final_url,
            "extract_rule": domain,
        },
    }
