"""Search-engine definitions: query URL templates + page-context extraction JS.

Engines are plain data — engine domain, a SERP URL template and an extraction
JS template — so the exact same shape can live in the plugin's rule database
(:mod:`rules`) and be edited at runtime through the ``camofox-search-manage``
tool. The built-ins here are both the seed values for that database (created
and seeded at plugin startup via ``INSERT OR IGNORE`` — edits are never
overwritten) and the fallback used when the rule store is unavailable.

Placeholders
------------
URL template:
    ``{query}`` — the search query, URL-encoded via ``quote_plus``;
    ``{limit}`` — the (clamped) result count.
Extraction JS (one expression evaluated in the page context):
    ``@LIMIT@`` — the same clamped result count.

The extraction expression must return
``JSON.stringify({"ok": true, "results": [{"title", "url", "description"}, ...]})``
so the provider can distinguish "no organic results" from "wrong page /
blocked / consent wall".

The Bing extractor is verified against live ``cn.bing.com`` markup
(``#b_results > li.b_algo`` items with ``h2 a`` links and ``.b_caption p``
snippets). Google and DuckDuckGo extractors are best-effort against their
current public markup and use multiple selector fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus


class UnknownEngineError(ValueError):
    """The configured engine id is not one this plugin implements."""


def render_url_template(template: str, query: str, limit: int) -> str:
    """Substitute ``{query}`` (URL-encoded) and ``{limit}`` in a URL template."""
    return (template.replace("{query}", quote_plus(query))
            .replace("{limit}", str(int(limit))))


def render_extract_js(template: str, limit: int) -> str:
    """Substitute the ``@LIMIT@`` count placeholder in an extraction expression."""
    return template.replace("@LIMIT@", str(int(limit)))


@dataclass(frozen=True)
class SearchEngine:
    """A built-in engine: canonical domain + URL and extraction templates."""

    name: str
    domain: str
    url_template: str
    js_template: str

    def build_url(self, query: str, limit: int) -> str:
        return render_url_template(self.url_template, query, limit)

    def extract_js(self, limit: int) -> str:
        return render_extract_js(self.js_template, limit)


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


ENGINES: dict[str, SearchEngine] = {
    "bing": SearchEngine(
        "bing", "bing.com",
        "https://www.bing.com/search?q={query}&count={limit}",
        _BING_JS_TEMPLATE),
    "google": SearchEngine(
        "google", "google.com",
        "https://www.google.com/search?q={query}&num={limit}",
        _GOOGLE_JS_TEMPLATE),
    "duckduckgo": SearchEngine(
        "duckduckgo", "duckduckgo.com",
        "https://duckduckgo.com/?q={query}&ia=web",
        _DDG_JS_TEMPLATE),
}


def get_engine(name: str) -> SearchEngine:
    """Look up an engine by id (case-insensitive); raises :class:`UnknownEngineError`."""
    engine = ENGINES.get((name or "").strip().lower())
    if engine is None:
        valid = ", ".join(sorted(ENGINES))
        raise UnknownEngineError(
            f"Unknown search engine {name!r}. Valid engines: {valid}.")
    return engine
