"""Camofox-backed web-search provider: search + page extraction through the
self-hosted Camofox anti-detection browser.

Every call opens its own *tab* inside one persistent, plugin-owned Camofox
profile (``camofox_user_id()``, default ``camofox_search``) and closes that tab
in a ``finally`` block. Keeping one stable profile means cookies imported by
``camofox-search-import-cookies`` (from the user's local browser) apply to
every search/extraction, while the daemon's own lifecycle still reclaims idle
contexts. Plugin activity never leaks into the browser sessions Hermes' own
``browser_*`` tools own.

Engine selection is rule-driven and multi-engine: ``CAMOFOX_SEARCH_ENGINE``
(default ``duckduckgo google bing``) holds the engine list; for every entry the
rule database (``HERMES_HOME/camofox-search/camofox-search.db``, see
:mod:`rules`) is consulted first — a rule matching the engine id/domain
supplies the SERP URL template and extraction JS — with the built-in engines
from :mod:`engines` as the fallback. All engines are searched **in parallel**
(one tab each) and their results are merged with hash-based URL deduplication
(:func:`merge_search_rows`). Content rules override the generic full-page text
extraction in :meth:`extract` for matching domains. Rules are editable at
runtime via the ``camofox-search-manage`` tool.

Response envelopes follow the Hermes legacy provider contract:

- ``search``:  ``{"success": True, "data": {"web": [{"title", "url",
  "description", "position"}, ...]}}``
- ``extract``: a **list** — ``[{"url", "title", "content", "raw_content",
  "metadata"}, ...]`` with ``error`` on a per-URL failure (the
  :class:`agent.web_search_provider.WebSearchProvider` contract the web_extract
  dispatcher consumes)
- failure:    ``{"success": False, "error": str}`` (search only)
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import urlsplit

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
from .settings import camofox_configured, camofox_user_id, default_engines

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

        plans, unknown = _search_plans(default_engines(), query, limit)
        if not plans:
            return {"success": False, "error": "; ".join(unknown) + _RULE_HINT}
        if unknown:
            logger.warning("camofox-search: skipping unknown configured "
                           "engine(s) for %r: %s", query, "; ".join(unknown))

        client = CamofoxClient()
        outcomes = _run_plans(client, plans, limit)
        healthy = [(label, rows) for label, rows, error in outcomes if error is None]
        if not healthy:
            first_error = next(error for _, _, error in outcomes if error is not None)
            return {"success": False, "error": first_error}
        failed = [label for label, _, error in outcomes if error is not None]
        if failed:
            logger.warning("camofox-search: engine(s) %s failed for %r; "
                           "returning partial merged results",
                           ", ".join(failed), query)
        rows = merge_search_rows([rows for _, rows in healthy], MAX_LIMIT)
        labels = ",".join(label for label, _ in healthy)
        logger.info("camofox-search %r via %s: %d merged result(s)",
                    query, labels, len(rows))
        return {"success": True, "data": {"web": rows}}

    # ---- extract ----

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        """Extract readable text for every URL; failures stay per-URL.

        Returns the list contract of :meth:`WebSearchProvider.extract` — the
        web_extract dispatcher iterates the value directly, so an envelope dict
        here raised ``'str' object has no attribute 'get'`` and lost every page.
        """
        del kwargs  # ignore format/include_raw/max_chars: one text rendering is served
        cleaned = [str(u).strip() for u in (urls or []) if str(u).strip()]
        if not cleaned:
            return []
        cleaned = cleaned[:MAX_EXTRACT_URLS]

        client = CamofoxClient()
        return [_extract_one(client, url) for url in cleaned]


def _search_plans(selectors: list[str], query: str,
                  limit: int) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Resolve ``(label, url, js_expression)`` plans for every engine selector.

    Rule database first — an engine id (``bing``), its domain (``bing.com``) or
    a subdomain (``cn.bing.com``) all resolve to the same rule — then the
    built-in engines. Returns ``(plans, unknown_errors)``: a selector matching
    neither is reported instead of aborting the other engines.
    """
    plans: list[tuple[str, str, str]] = []
    unknown: list[str] = []
    for selector in selectors:
        try:
            rule = _resolve_search_rule(selector)
            if rule is not None and rule.url_template:
                plans.append((
                    f"rule:{rule.domain}",
                    render_url_template(rule.url_template, query, limit),
                    render_extract_js(rule.extract_js, limit)))
                continue
            engine = get_engine(selector)
        except UnknownEngineError as exc:
            unknown.append(str(exc))
            continue
        plans.append((f"engine:{engine.name}",
                      engine.build_url(query, limit), engine.extract_js(limit)))
    return plans, unknown


def _run_plans(client: CamofoxClient, plans: list[tuple[str, str, str]],
               limit: int) -> list[tuple[str, list[dict[str, Any]], Optional[str]]]:
    """Run every plan in parallel; returns ``(label, rows, error)`` per engine.

    One worker thread per engine (each opens its own tab on the shared client),
    so a broken or slow engine costs its own retry budget instead of the whole
    search. An engine's :class:`CamofoxError` is captured for the caller rather
    than masking the other engines' results.

    Every worker runs inside a **copy of the caller's context**: the gateway's
    per-turn secret scope is a ``ContextVar`` that fresh threads do not
    inherit, so without the copy an env read inside a worker would fail closed
    (``UnscopedSecretError``) on a multiplexed gateway.
    """
    outcomes: list[tuple[str, list[dict[str, Any]], Optional[str]]] = []
    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=len(plans)) as pool:
        futures = [
            (label, pool.submit(context.copy().run, collect_search_rows,
                                client, url, expression, limit))
            for label, url, expression in plans
        ]
        for label, future in futures:
            try:
                outcomes.append((label, future.result(), None))
            except CamofoxError as exc:
                outcomes.append((label, [], str(exc)))
    return outcomes


def url_hash(url: str) -> str:
    """Dedupe hash for a result URL.

    Normalizes the variants different engines produce for the same page —
    scheme, leading ``www.``, default port, fragment and trailing slash — then
    hashes. Paths and query strings are compared as-is.
    """
    raw = str(url or "").strip()
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        try:
            port = parts.port
        except ValueError:
            port = None
        default_port = ((parts.scheme == "http" and port == 80)
                        or (parts.scheme == "https" and port == 443))
        if port and not default_port:
            host = f"{host}:{port}"
        base = host + parts.path.rstrip("/")
        if parts.query:
            base += "?" + parts.query
    except Exception:  # noqa: BLE001 - malformed URL: hash the raw text
        base = raw
    return hashlib.sha256(base.encode("utf-8", "replace")).hexdigest()


def merge_search_rows(results_by_engine: list[list[dict[str, Any]]],
                      cap: int) -> list[dict[str, Any]]:
    """Merge per-engine result lists: rank-interleaved, hash-deduped, capped.

    Engines contribute rows in their own ranking, interleaved rank-by-rank so
    one engine cannot monopolize the merged list. Rows whose URLs hash to the
    same page (:func:`url_hash`) merge into the first-seen row — empty title or
    description fields are filled from later copies. ``position`` is
    renumbered 1..N over the merged list, which is capped at ``cap`` rows.
    """
    merged: list[dict[str, Any]] = []
    by_hash: dict[str, dict[str, Any]] = {}
    rank = 0
    while len(merged) < cap:
        grew = False
        for rows in results_by_engine:
            if rank >= len(rows):
                continue
            grew = True
            row = rows[rank]
            key = url_hash(str(row.get("url") or ""))
            existing = by_hash.get(key)
            if existing is not None:
                for field in ("title", "description"):
                    if not existing[field] and str(row.get(field) or "").strip():
                        existing[field] = str(row[field]).strip()
                continue
            entry: dict[str, Any] = {
                "title": str(row.get("title") or "").strip(),
                "url": str(row.get("url") or "").strip(),
                "description": str(row.get("description") or "").strip(),
                "position": len(merged) + 1,
            }
            by_hash[key] = entry
            merged.append(entry)
            if len(merged) >= cap:
                break
        if not grew:
            break
        rank += 1
    return merged


def ensure_profile_tab(client: CamofoxClient, user_id: str) -> bool:
    """Keep one blank holder tab so the Camoufox window survives between calls.

    Closing a context's *last* tab destroys its browser window and every later
    ``Browser.newPage`` fails (``delayedStartupPromise, window is null``), so
    the stable plugin profile keeps one idle blank tab alive. Best effort —
    callers proceed regardless.
    """
    try:
        if client.list_tabs(user_id):
            return True
    except CamofoxError:
        return False
    try:
        client.create_tab(user_id)
        return True
    except CamofoxError as exc:
        logger.debug("camofox-search: holder tab for %s failed: %s", user_id, exc)
        return False


def open_page_tab(client: CamofoxClient, user_id: str, url: str) -> str:
    """Create the work tab for one call, healing a broken context exactly once.

    A context whose window died (keeper reaped, daemon restart) refuses new
    pages: the session is dropped and rebuilt before the single retry.
    """
    ensure_profile_tab(client, user_id)
    try:
        return client.create_tab(user_id, url)
    except CamofoxError:
        try:
            client.close_session(user_id)
        except Exception as exc:  # noqa: BLE001 - healing is best effort
            logger.debug("camofox-search: context reset for %s failed: %s", user_id, exc)
        ensure_profile_tab(client, user_id)
        return client.create_tab(user_id, url)


def collect_search_rows(client: CamofoxClient, url: str, expression: str,
                        limit: int, *, user_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Open ``url`` in a fresh tab of the plugin's profile and extract rows.

    Shared by :meth:`CamofoxSearchProvider.search` and the
    ``camofox-search-advance`` tool: create tab → wait for readiness → evaluate
    the extraction expression (retrying briefly when the DOM is not populated
    yet) → normalize into the ``web`` envelope rows. Only the *tab* is torn
    down afterwards — the browser context stays warm on the daemon and its
    persistent profile (``camofox_user_id()``) keeps cookies imported by
    ``camofox-search-import-cookies``. ``CamofoxError`` propagates to the
    caller.
    """
    user_id = user_id or camofox_user_id()
    tab_id: Optional[str] = None
    try:
        tab_id = open_page_tab(client, user_id, url)
        _await_ready(client, tab_id, user_id)
        rows: list[dict[str, Any]] = []
        for attempt in range(_SEARCH_EXTRACT_ATTEMPTS):
            payload = client.evaluate(tab_id, user_id, expression)
            rows = _result_rows(payload, limit)
            if rows or attempt == _SEARCH_EXTRACT_ATTEMPTS - 1:
                break
            time.sleep(_EXTRACT_RETRY_DELAY)
        return rows
    finally:
        _close_tab_quietly(client, tab_id, user_id)


def _close_tab_quietly(client: CamofoxClient, tab_id: Optional[str],
                       user_id: str) -> None:
    """Best-effort tab cleanup — never raises, never masks results."""
    if not tab_id:
        return
    try:
        client.close_tab(tab_id, user_id)
    except Exception as exc:  # noqa: BLE001 - deliberate best-effort cleanup
        logger.debug("Camofox tab cleanup failed for %s: %s", tab_id, exc)


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
    """Extract readable text from one URL in its own tab of the plugin profile.

    A content rule for the URL's host supplies the extraction expression; when
    it yields no usable content the generic full-page extraction runs instead.
    ``metadata.extract_rule`` records which path produced the content.
    """
    user_id = camofox_user_id()
    tab_id: Optional[str] = None
    try:
        tab_id = open_page_tab(client, user_id, url)
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
        _close_tab_quietly(client, tab_id, user_id)


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
