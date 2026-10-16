---
name: camofox-search-advance
description: "Run advanced camofox-search queries with the camofox-search-advance tool: choose a specific search engine and build (or execute) its SERP URL with operators (site:, filetype:, \"exact phrase\", -term, inurl:/intitle:/intext:, !bangs), time/language/region/vertical filters and pagination. Use when web_search's fixed engine is not enough, when a query needs freshness or language/region targeting, a news/images/academic vertical, WeChat or Chinese-engine coverage, a second independent engine for verification, or when the search URL itself must be constructed."
---

# camofox-search advanced search

`camofox-search-advance` (toolset `camofox-search`) builds the search URL for an
**explicitly chosen engine** — folding advanced operators into the query and
mapping the neutral filters onto that engine's own URL parameters — and then,
by default, executes the search through Camofox and extracts organic results.

- `mode: "run"` (default) — build **and execute**; the payload carries the
  queued URL plus the extracted results.
- `mode: "url"` — build only; hand the URL to `web_extract`, show it to the
  user, or reuse it. Use this when no extraction rule exists yet, or when the
  caller only wants the link.

## When to use this instead of web_search

- `web_search` always runs the *configured* engine. Use `camofox-search-advance`
  when the query needs a **specific** engine: Chinese vs international coverage,
  WeChat articles, privacy engines, or a second independent index for
  verification.
- When the query needs filters that exist only as URL parameters: freshness
  (hour/day/week/month/year or a custom date range), language, region, vertical
  (news/images/videos/maps/shopping/books/academic/scholar), result page.
- When the built URL itself is the deliverable.

Operators inside `query` keep working — `site:`, `filetype:`, `"exact
phrase"`, `-term`, `OR`, `*`, `..`, DuckDuckGo `!bangs`. The structured fields
(`site`, `filetype`, `exact`, `exclude`, `inurl`, `intitle`, `intext`) only
assemble that same operator text; they never become URL parameters of their own.

## Engine catalog

| engine id | engine | reach for it when |
|---|---|---|
| `baidu` | Baidu | Chinese general index; broadest Chinese coverage |
| `bing` | Bing (www.bing.com) | general default |
| `bing-cn` | Bing — `cn.bing.com` (`ensearch=0`) | Chinese results via Bing |
| `bing-int` | Bing — `cn.bing.com` (`ensearch=1`) | international results via the CN endpoint |
| `so360` | 360 Search | Chinese general (second opinion) |
| `sogou` | Sogou | Chinese general; strong Zhihu coverage |
| `sogou-wechat` | Sogou WeChat | WeChat Official Account articles (only channel) |
| `shenma` | Shenma | mobile-oriented Chinese content (Alibaba ecosystem) |
| `google` | Google | global general |
| `google-hk` | Google HK | Hong Kong / Traditional Chinese |
| `duckduckgo` | DuckDuckGo | privacy; bangs (`!gh`, `!so`, `!w`, `!a`, …) |
| `yahoo` | Yahoo | global general |
| `startpage` | Startpage | anonymous view of Google results |
| `brave` | Brave Search | independent index |
| `ecosia` | Ecosia | general |
| `qwant` | Qwant | EU privacy |
| `wolframalpha` | Wolfram\|Alpha | computation/knowledge — not a general results engine |

Aliases: `ddg` → `duckduckgo`, `360` → `so360`, `weixin`/`wechat` →
`sogou-wechat`. Domains are accepted too (`cn.bing.com`); a leading `www.` is
dropped. `bing-cn` and `bing-int` share the `cn.bing.com` domain — pass the id
to disambiguate.

## Option support matrix

`time` = hour | day | week | month | year or a custom range
`YYYY-MM-DD..YYYY-MM-DD`; `page` is 1-based; `limit` (1–20, default 10) caps
extracted results and is sent to engines with a size parameter.

| engine | time | language | region | vertical | page | limit |
|---|---|---|---|---|---|---|
| `baidu` | – | – | – | news, academic (dedicated endpoints) | ✓ (`pn`) | – |
| `bing`, `bing-cn`, `bing-int` | day/week/month (`ez` presets) | `setlang` | – | news, images, videos, academic | ✓ (`first`) | ✓ (`count`) |
| `so360` | – | – | – | – | ✓ (`pn`) | – |
| `sogou` | – | – | – | – | ✓ (`page`) | – |
| `sogou-wechat` | – | – | – | – | ✓ (`page`) | – |
| `shenma` | – | – | – | – | – | – |
| `google`, `google-hk` | ✓ (`tbs`) + range | `hl`+`lr` | `gl` | news/images/videos/shopping/books (`tbm`) + maps, scholar/academic (dedicated) | ✓ (`start`) | ✓ (`num`) |
| `duckduckgo` | day/week/month/year (`df`) + range | – | `kl` | web/news/images/videos (`ia`) | – | – |
| `yahoo` | – | – | – | – | ✓ (`b`) | – |
| `startpage` | day/week/month/year | – | – | – | – | – |
| `brave` | day/week/month/year (`tf`) | – | – | web/news/images/videos (`source`) | – | – |
| `ecosia`, `qwant`, `wolframalpha` | – | – | – | – | – | – |

An engine with no mapping **omits** the parameter — the response lists it under
`unsupported`. Read that field: a filter you needed may silently not have been
applied. Never hand-write a parameter the catalog says is unsupported; switch
engines or accept the loss instead.

## Run mode and extraction

Run mode resolves the extraction expression in this order:

1. a **search rule** for the engine's domain in the rule database
   (`camofox-search-manage`) — wins, always;
2. the **built-in extractor** for bing-, google-, duckduckgo-family engines;
3. neither — `executed: false`; the payload carries the built URL and a
   `reason`. Then either create a search rule (skill
   `web-camofox-search:camofox-search-rule`) or fetch the URL with `web_extract`.

The bing extractor matches the standard Bing result DOM; `ddg` extracts
best-effort; `google`/`google-hk` usually return **0 rows in run mode** — the
Google SERP renders slowly in this browser and wraps result links in opaque
`/goto?url=…` redirects, so treat `google` as URL-builder first (`mode:
"url"`) or use another engine for extraction. `executed: true` with `count: 0`
is **not** proof of "no results": the page may be empty, blocked, or the
extractor may not fit this vertical. Verify with `mode: "url"` or refresh the
rule before concluding.

## Workflows

1. Pick the engine by intent (catalog above; details in `references/`), pick
   options.
2. Call the tool. Check `unsupported` — for every filter listed, either accept
   the loss or pick an engine that supports it.
3. For fresh/live facts prefer `time: "day"`/`"week"` with `vertical: "news"`.
4. To verify a `web_search` result, re-run the same query on a second
   independent engine (`google` + `bing-int`, `ddg` + `brave`, `baidu` +
   `sogou`).
5. Zero extracted rows with a valid SERP URL: refresh the matching rule, don't
   repeat the same call.

Examples (the tool argument is this JSON):

```json
{"query": "hermes agent memory", "engine": "google", "time": "month", "limit": 10}
{"query": "深度学习 框架对比", "engine": "baidu", "site": "zhihu.com", "page": 2}
{"query": "机器之心", "engine": "sogou-wechat"}
{"query": "rust async runtime", "engine": "bing-int", "vertical": "news", "time": "week"}
{"query": "!gh tokio tasks", "engine": "ddg", "region": "us-en"}
{"query": "attention is all you need", "engine": "google", "vertical": "scholar", "time": "2024-01-01..2026-12-31", "mode": "url"}
{"query": "laptop reviews", "engine": "brave", "exact": "battery life", "exclude": ["sponsored"]}
```

## References

- `references/engines-cn.md` — Chinese engines in depth: operators that work,
  parameter behavior, what each result page returns.
- `references/engines-intl.md` — Google operators and parameters, DuckDuckGo
  bangs/params, Yahoo/Startpage/Brave/Ecosia/Qwant notes.

## Safety and upkeep

- One throwaway Camofox session per call; cookies/sessions of the user's own
  browser tools are never touched.
- Engine mappings live in `plugin/camofox_search/advanced.py`; when an engine
  changes its parameters, update the catalog there (and the pass in this
  skill). Rule changes take effect on the next call — no restart.
- When you create a rule to make a new engine extractable in run mode, put the
  why in `notes` so the next maintainer has the context.
