"""Advanced search URL construction: per-engine catalogs + neutral-filter mapping.

``camofox-search-advance`` builds a SERP URL for an explicitly chosen engine.
Two layers, per the ``web-ai-search`` catalog contract this module adapts:

* The query stays one *query*: advanced operators (``site:``, ``filetype:``,
  quoted phrases, ``-term``, Google-style ``inurl:``/``intitle:``/
  ``intext:``) are text inside it. The structured tool fields only assemble
  that text — they never become URL parameters of their own.
* Neutral filters — ``time``, ``language``, ``region``, ``vertical``,
  ``page``, ``limit`` — are *inputs*, not literal parameter names. Each
  engine maps them onto its own parameter names and value formats below. An
  engine with no mapping for a filter omits the parameter entirely (the
  caller reports it under ``unsupported``) — no invented parameters.

Time filters:

* Google family uses ``tbs=qdr:{h,d,w,m,y}`` and the custom range
  ``tbs=cdr:1,cd_min:M/D/YYYY,cd_max:M/D/YYYY``.
* Bing family uses ``filters=ex1:"ez{n}"`` (n = 1 day, 2 week, 3 month) —
  the 24-hours/1-week/1-month presets; ``hour`` is not offered.
* DuckDuckGo uses ``df={d,w,m,y}`` and the custom range
  ``df=YYYY-MM-DD..YYYY-MM-DD``; ``hour`` is not offered.
* Brave uses ``tf={pd,pw,pm,py}``; Startpage uses ``time=day|week|month|year``.

Status (verified on this deployment, camofox-browser 2.4.8):

* Bing ``filters=ex1:"ez{1,2,3}"`` is **live-verified** — with the parameter
  present the cn.bing.com SERP switches its filter bar into the active state.
* Google ``tbs=qdr:*``/``cdr`` and DuckDuckGo ``df=*`` survive into the
  rendered page (spot-checked; the engines keep the parameters in the URL).
* Google SERPs render an empty page in the Camofox browser here (JS shell,
  no result DOM), so the ``google``/``google-hk`` extractors are best-effort
  at best — expect 0 rows in run mode and prefer another engine or a rule.
* The Bing News/Images/Videos/Academic endpoints build valid URLs but may not
  render result cards in this environment — treat those verticals as
  URL-builder first, extractor second.
* Startpage/Sogou/Shenma/360 pagination mappings are documented-but-unverified
  — a wrong mapping fails soft (the engine ignores it or falls back) and is
  cheap to repair here.

``extractor`` names a built-in extraction template in :mod:`engines` for
run-mode result extraction; ``None`` means run mode needs a search rule for
the engine's domain (``camofox-search-manage``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional
from urllib.parse import quote_plus, urlencode, urlsplit

__all__ = [
    "AdvancedEngine", "BuiltSearch", "UnknownAdvancedEngineError",
    "AdvancedOptionError", "ADVANCED_ENGINES", "GLOBAL_VERTICALS",
    "get_advanced_engine", "engine_catalog", "compose_query",
    "build_engine_url",
]


class UnknownAdvancedEngineError(ValueError):
    """The requested engine id/domain is not in the advanced catalog."""


class AdvancedOptionError(ValueError):
    """An advanced option value is malformed (bad time range, page, limit)."""


# Global vertical vocabulary accepted by the tool; per-engine support varies.
GLOBAL_VERTICALS = (
    "web", "news", "images", "videos", "maps", "shopping", "books",
    "academic", "scholar",
)

_TIME_WORDS = ("hour", "day", "week", "month", "year")


@dataclass(frozen=True)
class AdvancedEngine:
    """One engine's advanced-search URL construction rules.

    ``params`` holds the base query parameters (one value is ``{query}``).
    The ``*_params`` dicts map neutral option names onto engine parameters;
    value templates reference ``{page}``/``{offset}``/``{first}``/``{limit}``/
    ``{language}``/``{region}``/``{time}``/``{vertical}``. ``time_map`` /
    ``vertical_map`` translate canonical values to engine values.
    ``vertical_urls`` replaces the base URL for verticals that live at a
    different path/host (the override template carries its own query param).
    """

    id: str
    name: str
    domain: str
    base_url: str
    params: dict[str, str] = field(default_factory=dict)
    page_params: Optional[dict[str, str]] = None
    limit_params: Optional[dict[str, str]] = None
    language_params: Optional[dict[str, str]] = None
    region_params: Optional[dict[str, str]] = None
    time_params: Optional[dict[str, str]] = None
    time_map: dict[str, str] = field(default_factory=dict)
    time_custom: Optional[str] = None  # "g-cdr" | "ddg-range"
    vertical_params: Optional[dict[str, str]] = None
    vertical_map: dict[str, str] = field(default_factory=dict)
    vertical_urls: dict[str, str] = field(default_factory=dict)
    extractor: Optional[str] = None  # built-in extractor id in engines.py
    notes: str = ""


_GOOGLE_TIME_MAP = {"hour": "h", "day": "d", "week": "w", "month": "m", "year": "y"}
_GOOGLE_VERTICAL_MAP = {
    "images": "isch", "news": "nws", "videos": "vid", "shopping": "shop",
    "books": "bks",
}
_GOOGLE_VERTICAL_URLS = {
    "maps": "https://www.google.com/maps/search/{query}",
    "scholar": "https://scholar.google.com/scholar?q={query}",
    "academic": "https://scholar.google.com/scholar?q={query}",
}

_BING_TIME_MAP = {"day": "1", "week": "2", "month": "3"}
_BING_TIME_PARAMS = {"filters": 'ex1:"ez{time}"'}

_DDG_TIME_MAP = {"day": "d", "week": "w", "month": "m", "year": "y"}
_DDG_VERTICAL_MAP = {
    "web": "web", "images": "images", "news": "news", "videos": "videos",
}
_BRAVE_TIME_MAP = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}
_BRAVE_VERTICAL_MAP = {
    "web": "web", "news": "news", "images": "images", "videos": "videos",
}


ADVANCED_ENGINES: dict[str, AdvancedEngine] = {}


def _register(engine: AdvancedEngine) -> AdvancedEngine:
    ADVANCED_ENGINES[engine.id] = engine
    return engine


_register(AdvancedEngine(
    id="baidu", name="Baidu", domain="baidu.com",
    base_url="https://www.baidu.com/s",
    params={"wd": "{query}"},
    page_params={"pn": "{offset}"},
    vertical_urls={
        "news": "https://news.baidu.com/ns?word={query}",
        "academic": "https://xueshu.baidu.com/s?wd={query}",
    },
    notes="Chinese general index; site:/filetype:/quotes work inside the query.",
))

_register(AdvancedEngine(
    id="bing", name="Bing", domain="bing.com",
    base_url="https://www.bing.com/search",
    params={"q": "{query}"},
    page_params={"first": "{first}"},
    limit_params={"count": "{limit}"},
    language_params={"setlang": "{language}"},
    time_params=_BING_TIME_PARAMS, time_map=_BING_TIME_MAP,
    vertical_urls={
        "news": "https://www.bing.com/news/search?q={query}",
        "images": "https://www.bing.com/images/search?q={query}",
        "videos": "https://www.bing.com/videos/search?q={query}",
        "academic": "https://www.bing.com/academic/search?q={query}",
    },
    extractor="bing",
    notes="Extractor verified against the Bing result DOM (li.b_algo); the "
          "news/images/videos/academic endpoints build valid URLs but may not "
          "render result cards in every environment — rule first there.",
))

_register(AdvancedEngine(
    id="bing-cn", name="Bing (Chinese results)", domain="cn.bing.com",
    base_url="https://cn.bing.com/search",
    params={"q": "{query}", "ensearch": "0"},
    page_params={"first": "{first}"},
    limit_params={"count": "{limit}"},
    language_params={"setlang": "{language}"},
    time_params=_BING_TIME_PARAMS, time_map=_BING_TIME_MAP,
    vertical_urls={
        "news": "https://cn.bing.com/news/search?q={query}",
        "images": "https://cn.bing.com/images/search?q={query}",
        "videos": "https://cn.bing.com/videos/search?q={query}",
        "academic": "https://cn.bing.com/academic/search?q={query}",
    },
    extractor="bing",
    notes="cn.bing.com with fixed ensearch=0 (Chinese results).",
))

_register(AdvancedEngine(
    id="bing-int", name="Bing (international results)", domain="cn.bing.com",
    base_url="https://cn.bing.com/search",
    params={"q": "{query}", "ensearch": "1"},
    page_params={"first": "{first}"},
    limit_params={"count": "{limit}"},
    language_params={"setlang": "{language}"},
    time_params=_BING_TIME_PARAMS, time_map=_BING_TIME_MAP,
    vertical_urls={
        "news": "https://cn.bing.com/news/search?q={query}",
        "images": "https://cn.bing.com/images/search?q={query}",
        "videos": "https://cn.bing.com/videos/search?q={query}",
        "academic": "https://cn.bing.com/academic/search?q={query}",
    },
    extractor="bing",
    notes="cn.bing.com endpoint with fixed ensearch=1 (international results).",
))

_register(AdvancedEngine(
    id="so360", name="360 Search", domain="so.com",
    base_url="https://www.so.com/s",
    params={"q": "{query}"},
    page_params={"pn": "{page}"},
    notes="Chinese general search; pagination mapping unverified.",
))

_register(AdvancedEngine(
    id="sogou", name="Sogou", domain="sogou.com",
    base_url="https://www.sogou.com/web",
    params={"query": "{query}"},
    page_params={"page": "{page}"},
    notes="Chinese general search; good Zhihu coverage.",
))

_register(AdvancedEngine(
    id="sogou-wechat", name="Sogou WeChat", domain="wx.sogou.com",
    base_url="https://wx.sogou.com/weixin",
    params={"type": "2", "query": "{query}"},
    page_params={"page": "{page}"},
    notes="WeChat Official Account article search (type=2); pagination "
          "mapping unverified.",
))

_register(AdvancedEngine(
    id="shenma", name="Shenma", domain="m.sm.cn",
    base_url="https://m.sm.cn/s",
    params={"q": "{query}"},
    notes="Mobile-oriented Chinese search (Alibaba ecosystem).",
))

_register(AdvancedEngine(
    id="google", name="Google", domain="google.com",
    base_url="https://www.google.com/search",
    params={"q": "{query}"},
    page_params={"start": "{offset}"},
    limit_params={"num": "{limit}"},
    language_params={"hl": "{language}", "lr": "lang_{language}"},
    region_params={"gl": "{region}"},
    time_params={"tbs": "qdr:{time}"}, time_map=_GOOGLE_TIME_MAP,
    time_custom="g-cdr",
    vertical_params={"tbm": "{vertical}"}, vertical_map=_GOOGLE_VERTICAL_MAP,
    vertical_urls=_GOOGLE_VERTICAL_URLS,
    extractor="google",
    notes="Renders an empty page in the Camofox browser (JS shell) — run mode "
          "usually returns 0 rows; prefer other engines or create a rule. "
          "Extractor is best-effort (multiple selector fallbacks).",
))

_register(AdvancedEngine(
    id="google-hk", name="Google (Hong Kong)", domain="google.com.hk",
    base_url="https://www.google.com.hk/search",
    params={"q": "{query}"},
    page_params={"start": "{offset}"},
    limit_params={"num": "{limit}"},
    language_params={"hl": "{language}", "lr": "lang_{language}"},
    region_params={"gl": "{region}"},
    time_params={"tbs": "qdr:{time}"}, time_map=_GOOGLE_TIME_MAP,
    time_custom="g-cdr",
    vertical_params={"tbm": "{vertical}"}, vertical_map=_GOOGLE_VERTICAL_MAP,
    vertical_urls={
        "maps": "https://www.google.com.hk/maps/search/{query}",
        "scholar": "https://scholar.google.com/scholar?q={query}",
        "academic": "https://scholar.google.com/scholar?q={query}",
    },
    extractor="google",
    notes="google.com.hk endpoint (Hong Kong / Traditional Chinese); same "
          "empty-render caveat as google.",
))

_register(AdvancedEngine(
    id="duckduckgo", name="DuckDuckGo", domain="duckduckgo.com",
    base_url="https://duckduckgo.com/",
    params={"q": "{query}", "ia": "web"},
    region_params={"kl": "{region}"},
    time_params={"df": "{time}"}, time_map=_DDG_TIME_MAP,
    time_custom="ddg-range",
    vertical_params={"ia": "{vertical}"}, vertical_map=_DDG_VERTICAL_MAP,
    extractor="duckduckgo",
    notes="Bangs (!g, !gh, !w, ...) stay inside the query. Extractor is "
          "best-effort; region uses the kl parameter.",
))

_register(AdvancedEngine(
    id="yahoo", name="Yahoo", domain="search.yahoo.com",
    base_url="https://search.yahoo.com/search",
    params={"p": "{query}"},
    page_params={"b": "{first}"},
))

_register(AdvancedEngine(
    id="startpage", name="Startpage", domain="startpage.com",
    base_url="https://www.startpage.com/sp/search",
    params={"query": "{query}"},
    time_params={"time": "{time}"},
    time_map={word: word for word in ("day", "week", "month", "year")},
    notes="Anonymous view of Google results; time mapping unverified.",
))

_register(AdvancedEngine(
    id="brave", name="Brave Search", domain="search.brave.com",
    base_url="https://search.brave.com/search",
    params={"q": "{query}"},
    time_params={"tf": "{time}"}, time_map=_BRAVE_TIME_MAP,
    vertical_params={"source": "{vertical}"}, vertical_map=_BRAVE_VERTICAL_MAP,
    notes="Independent index; tf presets and source verticals per Brave docs.",
))

_register(AdvancedEngine(
    id="ecosia", name="Ecosia", domain="ecosia.org",
    base_url="https://www.ecosia.org/search",
    params={"q": "{query}"},
))

_register(AdvancedEngine(
    id="qwant", name="Qwant", domain="qwant.com",
    base_url="https://www.qwant.com/",
    params={"q": "{query}"},
    notes="EU privacy engine; URL built, DOM varies by client rendering.",
))

_register(AdvancedEngine(
    id="wolframalpha", name="Wolfram|Alpha", domain="wolframalpha.com",
    base_url="https://www.wolframalpha.com/input",
    params={"i": "{query}"},
    notes="Computation/knowledge engine — not a general web-results engine.",
))

_ALIASES = {
    "360": "so360",
    "ddg": "duckduckgo",
    "wechat": "sogou-wechat",
    "weixin": "sogou-wechat",
}

# Domain form → engine (first registration wins for shared domains).
_DOMAIN_INDEX: dict[str, AdvancedEngine] = {}
for _engine in ADVANCED_ENGINES.values():
    _DOMAIN_INDEX.setdefault(_engine.domain, _engine)


def _normalize_selector(value: str) -> str:
    raw = str(value or "").strip().lower()
    if "://" in raw:
        raw = urlsplit(raw).hostname or ""
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    raw = raw.strip().strip(".")
    if raw.startswith("www."):
        raw = raw[4:]
    return raw


def get_advanced_engine(value: str) -> AdvancedEngine:
    """Look up an engine by id, alias or domain; raises when unknown.

    ``bing``/``bing.com`` and ``cn.bing.com`` resolve like the rule store:
    both id and domain forms are accepted, ``www.`` is dropped. Shared
    domains (``cn.bing.com`` backs both ``bing-cn`` and ``bing-int``) prefer
    the first-registered id; pass the id to disambiguate.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        raise UnknownAdvancedEngineError("engine is required")
    key = _ALIASES.get(raw, raw)
    engine = ADVANCED_ENGINES.get(key)
    if engine is None:
        engine = _DOMAIN_INDEX.get(_normalize_selector(raw))
    if engine is None:
        raise UnknownAdvancedEngineError(
            f"unknown advanced engine {value!r}; valid ids: "
            + ", ".join(sorted(ADVANCED_ENGINES)))
    return engine


def engine_catalog() -> list[dict[str, Any]]:
    """Compact catalog rows for tool output/errors: id, name, domain, notes."""
    return [
        {"id": engine.id, "name": engine.name, "domain": engine.domain,
         "extractor": engine.extractor, "notes": engine.notes}
        for engine in sorted(ADVANCED_ENGINES.values(), key=lambda e: e.id)
    ]


# ---- query composition ---------------------------------------------------


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    elif not isinstance(value, (list, tuple)):
        raise AdvancedOptionError(
            f"expected a string or a list of strings, got {type(value).__name__}")
    items: list[str] = []
    for entry in value:
        if entry is None:
            continue
        if not isinstance(entry, (str, int, float)):
            raise AdvancedOptionError(
                f"expected string entries, got {type(entry).__name__}")
        text = str(entry).strip()
        if text:
            items.append(text)
    return items


def compose_query(
    query: str,
    *,
    site: Optional[str] = None,
    filetype: Optional[str] = None,
    exact: Any = None,
    exclude: Any = None,
    inurl: Any = None,
    intitle: Any = None,
    intext: Any = None,
) -> str:
    """Assemble the final query string from the base query + operator fields.

    Word-operator order is stable: ``site:`` ``filetype:`` ``inurl:``
    ``intitle:`` ``intext:`` ``"exact"`` ``-term`` after the base query.
    """
    base = str(query or "").strip()
    if not base:
        raise AdvancedOptionError("query is required")
    parts = [base]
    site_text = str(site or "").strip()
    if site_text:
        parts.append(f"site:{site_text}")
    filetype_text = str(filetype or "").strip().lstrip(".").lower()
    if filetype_text:
        parts.append(f"filetype:{filetype_text}")
    for name, value in (("inurl", inurl), ("intitle", intitle), ("intext", intext)):
        for entry in _as_list(value):
            parts.append(f"{name}:{entry}")
    for phrase in _as_list(exact):
        parts.append('"' + phrase.replace('"', "").strip() + '"')
    for term in _as_list(exclude):
        parts.append("-" + term.lstrip("-").strip())
    return " ".join(part for part in parts if part)


# ---- URL building --------------------------------------------------------


@dataclass(frozen=True)
class BuiltSearch:
    """One fully-built advanced search request."""

    engine: AdvancedEngine
    url: str
    query: str  # final query including operators
    applied: dict[str, Any]  # option -> value that reached the URL
    unsupported: list[str]  # options the engine has no mapping for


def _parse_custom_range(value: str) -> tuple[date, date]:
    """``YYYY-MM-DD..YYYY-MM-DD`` → (start, end); raises on bad shape/order."""
    start_text, sep, end_text = value.partition("..")
    if not sep:
        raise AdvancedOptionError(
            f"invalid time value {value!r}: expected hour|day|week|month|year "
            "or a range like 2024-01-01..2024-12-31")
    try:
        start = date.fromisoformat(start_text.strip())
        end = date.fromisoformat(end_text.strip())
    except ValueError as exc:
        raise AdvancedOptionError(
            f"invalid time range {value!r}: dates must be YYYY-MM-DD") from exc
    if end < start:
        raise AdvancedOptionError(
            f"invalid time range {value!r}: end date precedes start date")
    return start, end


def _parse_page(value: Any) -> int:
    if value is None:
        return 1
    try:
        page = int(value)
    except (TypeError, ValueError) as exc:
        raise AdvancedOptionError(f"page must be an integer, got {value!r}") from exc
    if page < 1:
        raise AdvancedOptionError(f"page must be >= 1, got {page}")
    return page


def build_engine_url(
    engine: AdvancedEngine,
    query: str,
    *,
    time: Optional[str] = None,
    language: Optional[str] = None,
    region: Optional[str] = None,
    vertical: Optional[str] = None,
    page: Any = 1,
    limit: Any = None,
    site: Optional[str] = None,
    filetype: Optional[str] = None,
    exact: Any = None,
    exclude: Any = None,
    inurl: Any = None,
    intitle: Any = None,
    intext: Any = None,
) -> BuiltSearch:
    """Render the engine URL for ``query`` + options.

    Options without a mapping on this engine are *omitted* (never invented)
    and reported in ``unsupported``.
    """
    final_query = compose_query(
        query, site=site, filetype=filetype, exact=exact, exclude=exclude,
        inurl=inurl, intitle=intitle, intext=intext)
    page = _parse_page(page)
    limit_value: Optional[int] = None
    if limit is not None:
        try:
            limit_value = max(1, min(int(limit), 20))
        except (TypeError, ValueError) as exc:
            raise AdvancedOptionError(
                f"limit must be an integer, got {limit!r}") from exc

    applied: dict[str, Any] = {}
    unsupported: list[str] = []
    extra: dict[str, str] = {}

    # vertical: URL override > parameter mapping > unsupported
    base_url = engine.base_url
    override_used = False
    vertical_text = str(vertical or "").strip().lower()
    if vertical_text and vertical_text != "web":
        if vertical_text in engine.vertical_urls:
            base_url = engine.vertical_urls[vertical_text]
            override_used = True
            applied["vertical"] = vertical_text
        elif (engine.vertical_params
              and vertical_text in engine.vertical_map):
            mapped = engine.vertical_map[vertical_text]
            extra.update({name: tpl.format(vertical=mapped)
                          for name, tpl in engine.vertical_params.items()})
            applied["vertical"] = vertical_text
        else:
            unsupported.append("vertical")

    # time
    time_text = str(time or "").strip().lower()
    if time_text:
        if ".." in time_text:
            start, end = _parse_custom_range(time_text)
            if engine.time_custom == "g-cdr":
                extra["tbs"] = (f"cdr:1,cd_min:{start.month}/{start.day}/{start.year},"
                                f"cd_max:{end.month}/{end.day}/{end.year}")
                applied["time"] = time_text
            elif engine.time_custom == "ddg-range":
                extra["df"] = f"{start.isoformat()}..{end.isoformat()}"
                applied["time"] = time_text
            else:
                unsupported.append("time")
        elif time_text in _TIME_WORDS:
            if engine.time_params and time_text in engine.time_map:
                mapped = engine.time_map[time_text]
                extra.update({name: tpl.format(time=mapped)
                              for name, tpl in engine.time_params.items()})
                applied["time"] = time_text
            else:
                unsupported.append("time")
        else:
            raise AdvancedOptionError(
                f"invalid time value {time_text!r}: expected hour|day|week|month|"
                "year or a range like 2024-01-01..2024-12-31")

    # language / region
    language_text = str(language or "").strip()
    if language_text:
        if engine.language_params:
            extra.update({name: tpl.format(language=language_text)
                          for name, tpl in engine.language_params.items()})
            applied["language"] = language_text
        else:
            unsupported.append("language")
    region_text = str(region or "").strip()
    if region_text:
        if engine.region_params:
            extra.update({name: tpl.format(region=region_text)
                          for name, tpl in engine.region_params.items()})
            applied["region"] = region_text
        else:
            unsupported.append("region")

    # pagination / result count
    if page > 1:
        if engine.page_params:
            offset = (page - 1) * 10
            extra.update({name: tpl.format(page=page, offset=offset, first=offset + 1)
                          for name, tpl in engine.page_params.items()})
            applied["page"] = page
        else:
            unsupported.append("page")
    if limit_value is not None and engine.limit_params:
        extra.update({name: tpl.format(limit=limit_value)
                      for name, tpl in engine.limit_params.items()})
        applied["limit"] = limit_value

    if override_used:
        # Vertical override: the template carries its own query param; the
        # final query is embedded directly. Engine-wide option parameters
        # (if any were requested) append after it.
        url = base_url.replace("{query}", quote_plus(final_query))
        if extra:
            joiner = "&" if "?" in url else "?"
            url += joiner + urlencode(extra)
    else:
        params = {name: tpl.replace("{query}", final_query)
                  for name, tpl in engine.params.items()}
        params.update(extra)
        url = base_url + "?" + urlencode(params)

    return BuiltSearch(engine=engine, url=url, query=final_query,
                       applied=applied, unsupported=unsupported)
