"""Search-engine definitions: query URL builder + page-context extraction JS.

Each extractor is a single JS expression evaluated in the page context via the
Camofox ``/evaluate`` endpoint. It returns
``JSON.stringify({"ok": true, "engine": <name>, "results": [{"title", "url",
"description"}, ...]})`` so the provider can distinguish "no organic results"
from "wrong page / blocked / consent wall".

The Bing extractor is verified against live ``cn.bing.com`` markup
(``#b_results > li.b_algo`` items with ``h2 a`` links and ``.b_caption p``
snippets). Google and DuckDuckGo extractors are best-effort against their
current public markup and use multiple selector fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus


class UnknownEngineError(ValueError):
    """The configured engine id is not one this plugin implements."""


@dataclass(frozen=True)
class SearchEngine:
    name: str
    build_url: Callable[[str, int], str]
    extract_js: Callable[[int], str]


# ---- Bing (verified live against cn.bing.com) ----

_BING_JS_TEMPLATE = (
    "JSON.stringify({ok:true,engine:'bing',results:"
    "Array.from(document.querySelectorAll('#b_results > li.b_algo')).slice(0,@LIMIT@)"
    ".map(function(li){var a=li.querySelector('h2 a');"
    "var p=li.querySelector('.b_caption p')||li.querySelector('p');"
    "return {title:(a&&a.textContent||'').trim(),url:(a&&a.href)||'',"
    "description:(p&&p.textContent||'').trim()}})"
    ".filter(function(r){return r.url})})"
)


def _bing_url(query: str, limit: int) -> str:
    return f"https://www.bing.com/search?q={quote_plus(query)}&count={min(max(limit, 10), 30)}"


def _bing_extract(limit: int) -> str:
    return _BING_JS_TEMPLATE.replace("@LIMIT@", str(int(limit)))


# ---- Google (best effort) ----

_GOOGLE_JS_TEMPLATE = (
    "JSON.stringify({ok:true,engine:'google',results:"
    "Array.from(document.querySelectorAll('div.g')).filter(function(d){return d.querySelector('h3')})"
    ".slice(0,@LIMIT@)"
    ".map(function(d){var h=d.querySelector('h3');"
    "var a=h&&(h.closest('a')||d.querySelector('a[href^=\"http\"]'));"
    "var s=d.querySelector('.VwiC3b')||d.querySelector('[data-sncf]')||d.querySelector('div[style*=\"line-clamp\"]');"
    "return {title:(h&&h.textContent||'').trim(),url:(a&&a.href)||'',"
    "description:(s&&s.textContent||'').trim()}})"
    ".filter(function(r){return r.url&&r.title})})"
)


def _google_url(query: str, limit: int) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}&num={max(limit, 10)}"


def _google_extract(limit: int) -> str:
    return _GOOGLE_JS_TEMPLATE.replace("@LIMIT@", str(int(limit)))


# ---- DuckDuckGo (best effort) ----

_DDG_JS_TEMPLATE = (
    "JSON.stringify({ok:true,engine:'duckduckgo',results:"
    "Array.from(document.querySelectorAll("
    "'article[data-testid=\"result\"], li[data-layout=\"organic\"], div.result')).slice(0,@LIMIT@)"
    ".map(function(el){var a=el.querySelector('a[data-testid=\"result-title-a\"]')"
    "||el.querySelector('h2 a')||el.querySelector('a.result__a');"
    "var s=el.querySelector('[data-result=\"snippet\"]')||el.querySelector('.result__snippet');"
    "return {title:(a&&a.textContent||'').trim(),url:(a&&a.href)||'',"
    "description:(s&&s.textContent||'').trim()}})"
    ".filter(function(r){return r.url&&r.title})})"
)


def _duckduckgo_url(query: str, limit: int) -> str:
    return f"https://duckduckgo.com/?q={quote_plus(query)}&ia=web"


def _duckduckgo_extract(limit: int) -> str:
    return _DDG_JS_TEMPLATE.replace("@LIMIT@", str(int(limit)))


ENGINES: dict[str, SearchEngine] = {
    "bing": SearchEngine("bing", _bing_url, _bing_extract),
    "google": SearchEngine("google", _google_url, _google_extract),
    "duckduckgo": SearchEngine("duckduckgo", _duckduckgo_url, _duckduckgo_extract),
}


def get_engine(name: str) -> SearchEngine:
    """Look up an engine by id (case-insensitive); raises :class:`UnknownEngineError`."""
    engine = ENGINES.get((name or "").strip().lower())
    if engine is None:
        valid = ", ".join(sorted(ENGINES))
        raise UnknownEngineError(
            f"Unknown search engine {name!r}. Valid engines: {valid}.")
    return engine
