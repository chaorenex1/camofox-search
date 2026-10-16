"""Agent-facing tool: ``camofox-search-advance``.

Advanced web search through the local Camofox browser: build a SERP URL for an
*explicitly chosen* engine — folding advanced operators into the query
(``site:``, ``filetype:``, quoted phrases, ``-term``, ``inurl:``/``intitle:``/
``intext:``) and mapping the neutral time/language/region/vertical/page
filters onto that engine's own parameters (:mod:`advanced`) — then

- ``mode="run"`` (default): execute the URL through Camofox and extract the
  organic results. Extraction resolves *rules first*: a search rule for the
  engine's domain (``camofox-search-manage``) wins, then the built-in
  extractor for ``bing``/``google``/``duckduckgo``-family engines. With
  neither, the search is not executed and the payload carries the URL plus
  guidance — create a rule or fetch the URL with ``web_extract``.
- ``mode="url"``: return the built URL only (hand it to ``web_extract``,
  show it to the user, or reuse it in a later call).

Guidance ships as the bundled plugin skill
``web-camofox-search:camofox-search-advance`` (load it with ``skill_view``
before composing advanced queries).

Handler contract: returns a JSON *string*. Envelope:
``{"success": true, ...}`` or ``{"success": false, "error": "..."}``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import rules as rule_store
from .advanced import (
    GLOBAL_VERTICALS,
    AdvancedOptionError,
    UnknownAdvancedEngineError,
    build_engine_url,
    get_advanced_engine,
)
from .client import CamofoxClient, CamofoxError
from .engines import ENGINES, render_extract_js
from .provider import MAX_LIMIT, collect_search_rows
from .settings import default_engine

logger = logging.getLogger(__name__)

TOOL_NAME = "camofox-search-advance"

DEFAULT_LIMIT = 10

_HINT = ("Engine, option and workflow guidance: load the skill "
         "'web-camofox-search:camofox-search-advance' (skill_view).")

ADVANCE_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Advanced web search via the local Camofox browser: build a per-engine "
        "SERP URL (operators site:/filetype:/\"exact\"/-term/inurl:/intitle:/"
        "intext: folded into the query; time/language/region/vertical/page/limit "
        "mapped onto the engine's own parameters) and, by default, execute it "
        "and extract the organic results. Engines: baidu, bing, bing-cn, "
        "bing-int, so360, sogou, sogou-wechat (WeChat articles), shenma, "
        "google, google-hk, duckduckgo, yahoo, startpage, brave, ecosia, "
        "qwant, wolframalpha; accept ids, aliases (ddg, 360, weixin) or "
        "domains. mode='url' returns the built URL without executing. Options "
        "an engine does not support are omitted and reported in 'unsupported'. "
        "Run-mode extraction prefers a search rule for the engine domain "
        "(camofox-search-manage) over built-in extractors; with neither, the "
        "result carries the URL plus guidance (executed=false). "
        + _HINT
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search keywords. Advanced operators keep working inside the "
                    "query (site:example.com, filetype:pdf, \"exact phrase\", "
                    "-term, OR, DuckDuckGo !bangs); the structured operator "
                    "fields below are convenience builders for the same syntax."
                ),
            },
            "engine": {
                "type": "string",
                "description": (
                    "Catalog engine id, alias or domain (e.g. 'bing-cn', "
                    "'google', 'ddg', 'sogou-wechat', 'cn.bing.com'). Defaults "
                    "to the plugin's configured engine (the first entry of "
                    "CAMOFOX_SEARCH_ENGINE) when that engine is in the "
                    "advanced catalog."
                ),
            },
            "mode": {
                "type": "string",
                "enum": ["run", "url"],
                "description": (
                    "'run' (default) executes the search and returns extracted "
                    "results; 'url' returns only the built URL (e.g. to hand to "
                    "web_extract)."
                ),
            },
            "time": {
                "type": "string",
                "description": (
                    "Freshness filter: hour | day | week | month | year, or a "
                    "custom range 'YYYY-MM-DD..YYYY-MM-DD' (Google family and "
                    "DuckDuckGo). Mapped per engine (Google tbs=qdr:*, Bing "
                    "filters=ex1:\"ez*\", DuckDuckGo df=*, Brave tf=*, "
                    "Startpage time=*); unsupported engines report it in "
                    "'unsupported'."
                ),
            },
            "language": {
                "type": "string",
                "description": (
                    "Result/UI language code, e.g. 'zh-CN' or 'en'. Mapped where "
                    "the engine has a language parameter (Google hl/lr, Bing "
                    "setlang); otherwise reported in 'unsupported'."
                ),
            },
            "region": {
                "type": "string",
                "description": (
                    "Region code, e.g. 'cn', 'us' (DuckDuckGo accepts forms like "
                    "'us-en'). Mapped where supported (Google gl, DuckDuckGo "
                    "kl)."
                ),
            },
            "vertical": {
                "type": "string",
                "enum": list(GLOBAL_VERTICALS),
                "description": (
                    "Search vertical to target: web (default), news, images, "
                    "videos, maps, shopping, books, academic, scholar. Applied "
                    "as the engine's own vertical parameter or its dedicated "
                    "endpoint (Bing/Baidu/Google News, Google Scholar/Maps, "
                    "Bing Academic); engines without support report it in "
                    "'unsupported'."
                ),
            },
            "page": {
                "type": "integer",
                "minimum": 1,
                "description": (
                    "1-based result page; mapped per engine (Google start, Bing "
                    "first, Baidu pn, Sogou page, Yahoo b). Page 1 adds no "
                    "parameter. Engines without a mapping report it in "
                    "'unsupported'."
                ),
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LIMIT,
                "description": (
                    f"Max extracted results (1-{MAX_LIMIT}, default "
                    f"{DEFAULT_LIMIT}); also sent to engines that accept a size "
                    "parameter (Google num, Bing count)."
                ),
            },
            "site": {
                "type": "string",
                "description": "Restrict to a site: adds 'site:example.com' to the query.",
            },
            "filetype": {
                "type": "string",
                "description": "Restrict to a file type: adds 'filetype:pdf' (a leading dot is accepted).",
            },
            "exact": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "Phrase(s) that must appear verbatim: each becomes a quoted \"exact phrase\".",
            },
            "exclude": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "Term(s) to exclude: each becomes '-term'.",
            },
            "inurl": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "Word(s) that must appear in the result URL: each becomes 'inurl:word'.",
            },
            "intitle": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "Word(s) that must appear in the result title: each becomes 'intitle:word'.",
            },
            "intext": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "Word(s) that must appear in the page text: each becomes 'intext:word'.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}

_NO_EXTRACTOR_REASON = (
    "no extraction rule for {domain}: the URL was built but results cannot be "
    "extracted with the built-in extractors. Create a search rule with the "
    "camofox-search-manage tool (skill: 'web-camofox-search:camofox-search-rule'), "
    "fetch the URL with web_extract, or call again with mode='url'."
)

_EMPTY_RESULTS_NOTE = (
    "0 results extracted: the page may hold no organic results, may have hit a "
    "consent/bot wall, or its DOM may not match the extractor for this vertical. "
    "Verify with the built URL (mode='url') or refresh the search rule."
)


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error": message, **extra},
                      ensure_ascii=False)


def _string_arg(args: dict[str, Any], name: str) -> Optional[str]:
    if name not in args or args.get(name) is None:
        return None
    value = args.get(name)
    if not isinstance(value, str):
        raise AdvancedOptionError(f"{name} must be a string")
    return value


def handle_advance(args: dict[str, Any], **_kwargs: Any) -> str:
    """Tool handler: build (and optionally run) one advanced search."""
    args = args if isinstance(args, dict) else {}
    try:
        return _ok(**_dispatch(args))
    except (AdvancedOptionError, UnknownAdvancedEngineError) as exc:
        return _err(str(exc))
    except CamofoxError as exc:
        return _err(str(exc), engine=str(args.get("engine") or ""))
    except Exception as exc:  # noqa: BLE001 - tool handlers must never raise
        return _err(f"{type(exc).__name__}: {exc}")


def _resolve_engine(value: Optional[str]):
    """Explicit engine, or the configured one when it is in the catalog."""
    if value is not None and str(value).strip():
        return get_advanced_engine(str(value).strip())
    selector = default_engine()
    try:
        return get_advanced_engine(selector)
    except UnknownAdvancedEngineError as exc:
        raise AdvancedOptionError(
            f"configured engine {selector!r} is not in the advanced catalog; "
            f"pass 'engine' explicitly ({exc})") from exc


def _resolve_extractor(engine, limit: int) -> tuple[Optional[str], Optional[str]]:
    """``(js_expression, label)`` for run mode; rule first, then built-in."""
    rule = _search_rule_for(engine.domain)
    if rule is not None and rule.extract_js:
        return render_extract_js(rule.extract_js, limit), f"rule:{rule.domain}"
    if engine.extractor:
        builtin = ENGINES.get(engine.extractor)
        if builtin is not None:
            return builtin.extract_js(limit), f"engine:{engine.extractor}"
    return None, None


def _search_rule_for(domain: str):
    try:
        return rule_store.resolve_search_rule(domain)
    except Exception as exc:  # noqa: BLE001 - rule trouble must not break search
        logger.warning(
            "camofox-search: advance rule lookup for %r failed "
            "(falling back to built-in extractors): %s", domain, exc)
        return None


def _dispatch(args: dict[str, Any]) -> dict[str, Any]:
    query = _string_arg(args, "query")
    if query is None or not query.strip():
        raise AdvancedOptionError("query is required")
    engine = _resolve_engine(_string_arg(args, "engine"))

    mode = str(args.get("mode") or "run").strip().lower()
    if mode not in ("run", "url"):
        raise AdvancedOptionError(
            f"invalid mode {mode!r}; expected 'run' or 'url'")

    limit = args.get("limit")
    if limit is None:
        limit = DEFAULT_LIMIT
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError) as exc:
        raise AdvancedOptionError(
            f"limit must be an integer, got {limit!r}") from exc

    built = build_engine_url(
        engine, query,
        time=_string_arg(args, "time"),
        language=_string_arg(args, "language"),
        region=_string_arg(args, "region"),
        vertical=_string_arg(args, "vertical"),
        page=args.get("page", 1),
        limit=limit,
        site=_string_arg(args, "site"),
        filetype=_string_arg(args, "filetype"),
        exact=args.get("exact"),
        exclude=args.get("exclude"),
        inurl=args.get("inurl"),
        intitle=args.get("intitle"),
        intext=args.get("intext"),
    )

    expression, extractor = _resolve_extractor(engine, limit)
    payload: dict[str, Any] = {
        "mode": mode,
        "engine": engine.id,
        "engine_name": engine.name,
        "domain": engine.domain,
        "url": built.url,
        "query": built.query,
        "applied": built.applied,
        "unsupported": built.unsupported,
        "limit": limit,
        "extractor": extractor,
    }
    if engine.notes:
        payload["engine_notes"] = engine.notes

    if mode == "url":
        payload["hint"] = (f"Fetch this URL with web_extract, or re-run with "
                           f"mode='run'. {_HINT}")
        return payload

    if expression is None:
        payload.update({
            "executed": False,
            "count": 0,
            "results": [],
            "reason": _NO_EXTRACTOR_REASON.format(domain=engine.domain),
        })
        return payload

    client = CamofoxClient()
    rows = collect_search_rows(client, built.url, expression, limit)
    payload.update({"executed": True, "count": len(rows), "results": rows})
    if not rows:
        payload["note"] = _EMPTY_RESULTS_NOTE
    return payload
