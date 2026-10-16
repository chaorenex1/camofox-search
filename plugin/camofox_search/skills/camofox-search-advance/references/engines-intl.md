# International engines — deep reference

Reference for the `google` / `google-hk` / `duckduckgo` / `yahoo` /
`startpage` / `brave` / `ecosia` / `qwant` / `wolframalpha` engines of
`camofox-search-advance`. Adapted from the web-ai-search international-engine
guide; everything listed is mapped by the tool — unsupported options are
reported in `unsupported` and must not be hand-written into URLs.

## Google (`google`, `google-hk`)

### Query operators (kept inside `query`)

| operator | meaning | example |
|---|---|---|
| `""` | exact phrase | `"machine learning"` |
| `-` | exclude | `python -snake` |
| `OR` | either | `machine learning OR deep learning` |
| `*` | wildcard word | `machine * algorithms` |
| `()` | grouping | `(apple OR microsoft) phones` |
| `..` | number range | `laptop $500..$1000` |
| `site:` | restrict to site | `site:github.com python projects` |
| `filetype:` | file type | `filetype:pdf annual report` |
| `inurl:` | word in URL | `inurl:login admin` |
| `intitle:` | word in title | `intitle:"index of" mp3` |
| `intext:` | word in body | `intext:password filetype:txt` |

Retired by Google — do not rely on them: `cache:`, `info:`, `related:`
(they no longer return meaningful results).

### Tool-mapped parameters

| option | parameter | values |
|---|---|---|
| `time` | `tbs=qdr:*` | hour→`h`, day→`d`, week→`w`, month→`m`, year→`y` |
| `time` custom range | `tbs=cdr:1,cd_min:M/D/YYYY,cd_max:M/D/YYYY` | `YYYY-MM-DD..YYYY-MM-DD` in the tool |
| `language` | `hl={code}` + `lr=lang_{code}` | `zh-CN`, `en`, … |
| `region` | `gl={code}` | `cn`, `us`, … |
| `vertical` | `tbm=` | images→`isch`, news→`nws`, videos→`vid`, shopping→`shop`, books→`bks` |
| `vertical` dedicated endpoint | see below | maps, scholar, academic |
| `page` | `start=` | 10 results per page (page 2 → `start=10`) |
| `limit` | `num=` | max 20 in this tool |

Vertical endpoints (URL override, so engine-wide params still apply):

- `vertical: "maps"` → `https://www.google.com/maps/search/{query}`
- `vertical: "scholar"` / `"academic"` → `https://scholar.google.com/scholar?q={query}`

Notes:

- `google-hk` is the same mapping family on `google.com.hk` (HK / Traditional
  Chinese results).
- Google in the Camofox browser: SERPs render slowly (JS) and result links are
  wrapped in opaque `google.com/goto?url=…` redirects, so run-mode extraction
  usually yields 0 rows. Use `mode: "url"` (fetch with `web_extract`) or
  another engine; a search rule only helps if it targets the extractable
  markup of the page.
- Scholar pages are a different DOM than the main SERP — expect to create a
  rule (or `mode: "url"` + `web_extract`) before run mode works there.

Examples:

```json
{"query": "site:github.com python machine learning", "engine": "google"}
{"query": "machine learning tutorial", "engine": "google", "filetype": "pdf", "time": "2024-01-01..2026-12-31"}
{"query": "AI breakthrough", "engine": "google", "time": "week", "vertical": "news"}
{"query": "人工智能", "engine": "google", "language": "zh-CN", "region": "cn"}
{"query": "deep learning optimization", "engine": "google", "vertical": "scholar", "mode": "url"}
```

## DuckDuckGo (`duckduckgo`)

### Bangs (stay inside `query`)

| category | bangs |
|---|---|
| search engines | `!g` Google, `!b` Bing, `!y` Yahoo, `!sp` Startpage, `!brave` Brave |
| dev | `!gh` GitHub, `!so` Stack Overflow, `!npm` npm, `!pypi` PyPI, `!mdn` MDN, `!docs` DevDocs, `!docker` Docker Hub |
| knowledge | `!w` Wikipedia, `!wen` Wikipedia (EN), `!wt` Wiktionary, `!imdb` IMDb |
| shopping | `!a` Amazon, `!e` eBay, `!ali` AliExpress |
| places | `!m` Google Maps, `!maps` OpenStreetMap |

### Tool-mapped parameters

| option | parameter | values |
|---|---|---|
| `region` | `kl=` | `us-en`, `cn`, … |
| `time` | `df=` | day→`d`, week→`w`, month→`m`, year→`y` |
| `time` custom range | `df=YYYY-MM-DD..YYYY-MM-DD` | `YYYY-MM-DD..YYYY-MM-DD` |
| `vertical` | `ia=` | web, news, images, videos |

Not mapped by the tool: `kp` (safe search), `s` (offset pagination) — the
built-in engine URL already fixes `ia=web`.

Notes: bangs redirect straight to the target engine (`!gh react hooks` →
GitHub search); the extractor is best-effort, so heavy bang use is more
reliably followed with `mode: "url"` + `web_extract`.

## Other engines — quick reference

| engine | base URL (tool renders) | mapped options |
|---|---|---|
| `yahoo` | `https://search.yahoo.com/search?p={query}` | `page` → `b={first}` |
| `startpage` | `https://www.startpage.com/sp/search?query={query}` | `time` → `time=day|week|month|year` |
| `brave` | `https://search.brave.com/search?q={query}` | `time` → `tf=pd|pw|pm|py`; `vertical` → `source=web|news|images|videos` |
| `ecosia` | `https://www.ecosia.org/search?q={query}` | — |
| `qwant` | `https://www.qwant.com/?q={query}` | — |
| `wolframalpha` | `https://www.wolframalpha.com/input?i={query}` | — (computation/knowledge box, not a results list) |

Notes:

- Startpage returns Google results anonymously; its time mapping is
  documented-but-unverified — spot-check the filter chip before trusting it.
- Brave has an independent index — a good second opinion against Google/Bing.
- Qwant is client-rendered; `mode: "url"` is often more useful than run mode.
- Wolfram|Alpha evaluates the query as a computation (`integrate x^2 dx`) —
  use it for math/units/data questions, not for link discovery.

## Verification habit

International SERPs and consent walls change often. When run mode returns
nothing but `mode: "url"` shows a valid page, the extractor mapping is stale —
fix the search rule for that domain (skill `camofox-search-rule`) instead of
retrying the same call.
