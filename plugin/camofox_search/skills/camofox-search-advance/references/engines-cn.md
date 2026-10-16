# Chinese engines — deep reference

Reference for the `baidu` / `bing-cn` / `bing-int` / `so360` / `sogou` /
`sogou-wechat` / `shenma` engines of `camofox-search-advance`. Adapted from the
web-ai-search Chinese-engine guide; all parameters below are ones the tool
maps — options not listed are reported in `unsupported` and must not be
hand-written into URLs.

## Baidu (`baidu`)

| capability | how |
|---|---|
| Base search | `https://www.baidu.com/s?wd={query}` |
| Pagination | `page: 2+` → `&pn=10` (offset, 10 per page) |
| Operators in query | `site:`, `filetype:`, `"exact phrase"`, `-term` (e.g. `机器学习 filetype:pdf`) |
| News vertical | `vertical: "news"` → `https://news.baidu.com/ns?word={query}` |
| Academic vertical | `vertical: "academic"` → `https://xueshu.baidu.com/s?wd={query}` (Baidu Xueshu) |
| Not mapped | time, language, region (`unsupported`) |

Notes:

- Chinese index with the broadest coverage; queries in Chinese work best.
- `site:` + `filetype:` combine well: `深度学习 site:github.com filetype:ipynb`.
- Baidu rewrites result links via redirect URLs; open the returned link or
  match on the title when cross-checking.

Example:

```json
{"query": "深度学习 框架对比", "engine": "baidu", "site": "zhihu.com", "page": 2}
```

## Bing CN / international (`bing`, `bing-cn`, `bing-int`)

| capability | how |
|---|---|
| Base search | `https://cn.bing.com/search?q={query}&ensearch=0` (CN) / `ensearch=1` (intl.) |
| Pagination | `page: 2+` → `&first=11` (1-based item offset) |
| Result count | `limit` → `&count={limit}` |
| Interface language | `language` → `&setlang=zh-CN` |
| Freshness | `time: "day"` → `&filters=ex1:"ez1"`; `week` → `ez2`; `month` → `ez3` (presets only) |
| Verticals | `news` → `/news/search`, `images` → `/images/search`, `videos` → `/videos/search`, `academic` → `/academic/search` (all on `cn.bing.com`) |
| Not mapped | region (`unsupported`) |

Notes:

- `ensearch` picks the result language family on the CN endpoint: `0` =
  Chinese results, `1` = international results — the same server either way.
- Bing is the plugin's default engine; `bing` (www) shares the verified
  `li.b_algo` extractor with the CN pages.
- The News/Images/Videos/Academic endpoints build valid URLs, but in this
  deployment they may not render result cards for extraction — use them as
  `mode: "url"` builders or add a search rule for their DOM.
- Bing Academic (`vertical: "academic"`) indexes papers — useful for
  scholarly material without Google Scholar access.

Example:

```json
{"query": "大语言模型 综述", "engine": "bing-cn", "time": "month", "limit": 10}
```

## 360 Search (`so360`)

| capability | how |
|---|---|
| Base search | `https://www.so.com/s?q={query}` |
| Pagination | `page: 2+` → `&pn={page}` (page number; unverified — spot-check the result count) |
| Operators in query | `site:`, quotes (e.g. `site:zhihu.com python`) |
| Not mapped | time, language, region, vertical |

Notes: safe-search is on by default; useful as a third Chinese index when
Baidu/Sogou results disagree.

## Sogou (`sogou`)

| capability | how |
|---|---|
| Base search | `https://www.sogou.com/web?query={query}` |
| Pagination | `page: 2+` → `&page={page}` |
| Operators in query | `site:` works well with `site:zhihu.com` (Sogou indexes Zhihu deeply) |
| Not mapped | time, language, region, vertical |

Example:

```json
{"query": "site:zhihu.com 机器学习 入门", "engine": "sogou"}
```

## Sogou WeChat (`sogou-wechat`)

| capability | how |
|---|---|
| Base search | `https://wx.sogou.com/weixin?type=2&query={query}` (`type=2` = article search, fixed) |
| Pagination | `page: 2+` → `&page={page}` (unverified mapping) |
| Search by account | put `公众号:NAME` in the query, e.g. `公众号:机器之心` |
| Not mapped | everything else |

Notes: the only channel for searching WeChat Official Account articles. Titles
and snippets come back from the WeChat index; full articles open under
`mp.weixin.qq.com`. No run-mode extractor ships for `wx.sogou.com` — create a
search rule first (see the `camofox-search-rule` skill) or use `mode: "url"`
plus `web_extract`.

## Shenma (`shenma`)

| capability | how |
|---|---|
| Base search | `https://m.sm.cn/s?q={query}` |
| Not mapped | everything else |

Notes: mobile-oriented results (Alibaba/UC ecosystem); good for content that
skews mobile-only. No run-mode extractor — rule or `mode: "url"` first.

## Choosing an engine by intent

| goal | first choice | why |
|---|---|---|
| General Chinese content | `baidu` | broadest Chinese index |
| WeChat articles | `sogou-wechat` | only supported channel |
| Zhihu content | `sogou` with `site:zhihu.com` | deep Zhihu indexing |
| Mobile-first content | `shenma` | mobile index (Ali ecosystem) |
| Academic material | `bing-cn` `vertical: "academic"` or `baidu` `vertical: "academic"` | Bing Academic / Baidu Xueshu |
| Chinese ↔ international bilingual | `bing-cn` / `bing-int` | same endpoint, `ensearch` switch |
| News | `baidu` `vertical: "news"` / `bing` `vertical: "news"` + `time` | dedicated news verticals |

## Verification habit

Chinese SERPs change markup often. When run mode returns nothing but
`mode: "url"` shows a valid page, the extractor mapping is stale — fix the
search rule for that domain (skill `camofox-search-rule`) rather than
retrying the same call.
