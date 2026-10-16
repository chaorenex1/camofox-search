# Hermes camofox-search plugin

A Hermes web-search provider plugin that services `web_search` and `web_extract`
through a self-hosted [Camofox](https://github.com/jo-inc/camofox-browser)
anti-detection browser (Camoufox engine) instead of a hosted search API.

- Provider name: `camofox-search` (select via `web.search_backend` / `web.extract_backend`)
- Plugin key: `web/camofox_search`
- No API keys of its own: it uses the local Camofox server (`CAMOFOX_URL`, `CAMOFOX_API_KEY`)
- Rule-driven: search engines and page extraction are defined in a SQLite
  database, editable at runtime via the bundled `camofox-search-manage` tool
- Multi-engine search: `CAMOFOX_SEARCH_ENGINE` lists engines (default
  `duckduckgo google bing`); every one is searched in parallel and the results
  are merged with hash-based URL deduplication
- Advanced search: the bundled `camofox-search-advance` tool builds per-engine
  SERP URLs (operators, time/language/region/vertical/pagination filters) and
  can execute them — see "Advanced search" below
- Cookies: the bundled `camofox-search-import-cookies` tool (plus a
  "every 72h" scheduled refresh) copies the user's local-browser cookies into
  the plugin's Camofox profile — see "Cookies" below

## How it works

Every call opens its own *tab* inside one stable, plugin-owned Camofox profile
(`CAMOFOX_SEARCH_USER_ID`, default `camofox_search`): search navigates the tab
to the selected engine's result page, extracts organic results from the page
DOM via the Camofox `/evaluate` endpoint, and closes the tab in a `finally`
block; `extract()` does the same per URL and returns readable page text. One
stable profile means cookies imported from the local browser (see "Cookies"
below) apply to every search/extraction, and the daemon's own lifecycle
reclaims idle contexts. Plugin activity never leaks into the browser sessions
Hermes' own `browser_*` tools own.

The profile keeps one idle blank *holder* tab: Camoufox destroys its browser
window when the last tab closes and then refuses new pages, so the holder is
what lets per-call tabs come and go. A context that still ends up broken
(daemon restart, reaped holder) is reset and retried once, transparently.

Engine selection is rule-driven and multi-engine: `CAMOFOX_SEARCH_ENGINE` holds
a whitespace/comma-separated engine list (default `duckduckgo google bing`).
For every entry the rule database is consulted first — a rule matching the
engine id or domain resolves the SERP URL template and extraction JS (`bing`,
`bing.com` and `cn.bing.com` all resolve to the same rule; longest domain wins)
— with the built-in engines (`duckduckgo`, `google`, `bing`) as the fallback.
All engines are searched **in parallel** (one tab each on the shared profile)
and their results are merged: duplicates (same URL modulo scheme/`www.`/
fragment/trailing slash, keyed by hash) collapse into one row, engines
interleave by rank, and the merged list is capped at 20 rows (Hermes trims it
to the caller's count). An engine that fails or matches no rule is skipped
while the others still contribute. The Bing extractor is verified against
`cn.bing.com`; `google`/`duckduckgo` extractors are best-effort.

## Rules (search + content)

Data lives at `$HERMES_HOME/camofox-search/` (override with
`CAMOFOX_SEARCH_DATA_DIR`); database `camofox-search.db` (WAL):

| Table | Columns | Drives |
|---|---|---|
| `search_rules` | `domain`, `url_template`, `extract_js`, `notes`, timestamps | `web_search` engine selection |
| `content_rules` | `domain`, `extract_js`, `notes`, timestamps | `web_extract` page extraction |

- **Search rule** — `url_template` with `{query}` (URL-encoded) and optional
  `{limit}` placeholders; `extract_js` is one page-context expression returning
  `JSON.stringify({ok:true,results:[{title,url,description}]})` (`@LIMIT@` is
  replaced with the requested count). Rules extend the engine set: create a
  rule for `yandex.com`, add `yandex` to `CAMOFOX_SEARCH_ENGINE`, done.
- **Content rule** — `extract_js` returns
  `JSON.stringify({ok:true,title,url,text})` or a plain string. Matches the
  domain and every subdomain (longest match wins); an unusable rule result
  falls back to the default full-page text extraction, and the result metadata
  records the path (`metadata.extract_rule`, plus `extract_rule_attempted` on
  fallback).
- The rule database is created and the built-in engines (`duckduckgo`,
  `google`, `bing`) are seeded into `search_rules` at plugin startup
  (`INSERT OR IGNORE` — edits are never overwritten), so the engine rules
  always exist and the table doubles as documentation of the default behavior.
- Rule trouble (unwritable DB, corrupt file) degrades gracefully: search falls
  back to the built-in engines, extraction to the default reader.

**Tool** — `camofox-search-manage` (toolset `camofox-search`): `action` =
`list | get | create | update | delete`, `rule_type` = `search | content`, plus
`domain`, `url_template`, `extract_js`, `notes`. `update` merges only the
fields passed. The next `web_search`/`web_extract` call picks up changes — no
restart.

**Skill** — `camofox-search-rule` ships with the plugin and loads as
`web-camofox-search:camofox-search-rule` (e.g. `skill_view`). It teaches the
agent to maintain rules proactively: diagnose a failed search/extraction,
author and store a rule, verify it, iterate — and to create a rule when a
search or content URL has no matching rule yet.

## Advanced search

**Tool** — `camofox-search-advance` (toolset `camofox-search`) builds the SERP
URL for an explicitly chosen engine and, by default, executes it through
Camofox. Engines: `baidu`, `bing`, `bing-cn`, `bing-int`, `so360`, `sogou`,
`sogou-wechat`, `shenma`, `google`, `google-hk`, `duckduckgo`, `yahoo`,
`startpage`, `brave`, `ecosia`, `qwant`, `wolframalpha` (ids, aliases like
`ddg`/`360`/`weixin`, or domains).

- Query operators stay in the query (`site:`, `filetype:`, `"exact phrase"`,
  `-term`, `inurl:`/`intitle:`/`intext:`, DuckDuckGo bangs); the structured
  tool fields only assemble that text. Neutral filters — `time`, `language`,
  `region`, `vertical`, `page`, `limit` — are mapped onto each engine's own
  parameters; an engine without a mapping omits the parameter and reports it
  in `unsupported` instead of inventing one.
- `mode: "run"` (default) extracts results through the same rule-driven
  pipeline: a search rule for the engine's domain wins, then the built-in
  bing/google/duckduckgo extractors. With neither, the payload carries the
  built URL and guidance (`executed: false`) — create a rule via
  `camofox-search-manage` or fetch the URL with `web_extract`.
- `mode: "url"` returns the built URL only.
- Verified on a live camofox-browser 2.4.8: Bing `filters=ex1:"ez*"` (the SERP
  switches into the filtered state), DDG/Brave param retention, and
  url-correctness of every catalog engine. Known limits: Google SERPs render
  slowly and wrap links in opaque redirects (0 rows in run mode — use
  `mode: "url"` or another engine); Bing news/images/videos/academic endpoints
  may not render result cards.

**Skill** — `camofox-search-advance` (loads as
`web-camofox-search:camofox-search-advance`) documents the engine catalog,
the option support matrix, extraction resolution and workflows, with
per-engine deep references in `references/engines-cn.md` and
`references/engines-intl.md`.

## Cookies (local browser → Camofox)

**Tool** — `camofox-search-import-cookies` copies cookies from the user's own
browser into the Camofox profile the plugin browses with, so searches and
extractions carry the user's logins / consent / clearance cookies.

- `action: "detect"` — lists every detected browser profile with a cookie
  database (Chrome, Edge, Brave, Chromium, Vivaldi, Firefox under the standard
  Windows paths) plus whether a CDP fallback port answers.
- `action: "run"` — exports the chosen profile (`browser`, `profile`;
  `auto` = most recently used; optional `domains` suffix filter), imports it
  in ≤500-cookie batches into `user_id` (default the plugin's profile) and
  verifies the round trip on a scratch tab. `dry_run: true` stops after the
  export. Only counts and skip reasons are returned — never cookie values.
- Sources and limits: Chromium cookies are AES-256-GCM values under a
  DPAPI-protected key in `Local State` (current builds add a 32-byte binding
  prefix that is stripped). App-bound `v20` values (modern Edge/Chrome) export
  through the optional [`rookie-cookies`](https://github.com/teng-lin/rookie-cookies)
  package (`pip install rookie-cookies`, or the `abe` extra): it recovers the
  app-bound key inside a browser process it spawns — the target browser must
  be closed; no elevation needed. `CAMOFOX_SEARCH_COOKIE_APP_BOUND=disabled`
  skips v20 rows instead of injecting. Without the package those rows are
  skipped. Firefox `cookies.sqlite` is plaintext. A *running* Chromium-family
  browser locks its cookie DB — the tool then falls back to a CDP export on
  `127.0.0.1:9222` (override `CAMOFOX_SEARCH_CDP_PORTS`) when a browser with
  `--remote-debugging-port` is reachable, and otherwise reports exactly what
  to close or re-run.

**Scheduled refresh** — `scripts/camofox_cookie_sync.py` (deployed to
`$HERMES_HOME/scripts/` by the installer) runs the same import and prints a
one-line, value-free summary; a `no_agent` cron job ("every 72h") executes it.
When the local browser is open and no CDP port answers, the run exits non-zero
with the actionable message so the job history shows it — close the browser
(or enable a debug port) before the next fire.

## Layout

```
plugin/camofox_search/          Plugin source (install target: $HERMES_HOME/plugins/web/camofox_search)
  plugin.yaml                   Manifest: kind=backend, provides_web_providers, provides_tools
  __init__.py                   register(ctx): provider + tools + bundled skills
  provider.py                   CamofoxSearchProvider (rule-aware search/extract)
  client.py                     Camofox REST client (tab lifecycle, cookies, Bearer auth)
  engines.py                    Built-in engines as data: domain + URL/JS templates
  advanced.py                   Advanced engine catalog + URL builder (operators/filters)
  rules.py                      SQLite rule store ($HERMES_HOME/camofox-search/, WAL)
  manage.py                     camofox-search-manage tool (rules CRUD)
  advance.py                    camofox-search-advance tool (build/run advanced searches)
  cookies.py                    Local-browser cookie export (DPAPI/AES-GCM, CDP) + import
  import_cookies.py             camofox-search-import-cookies tool
  settings.py                   Env resolution (secret scope -> process env -> Hermes .env)
  skills/camofox-search-rule/   Bundled skill (rules maintenance)
  skills/camofox-search-advance/ Bundled skill (engine catalog + references/)
scripts/install_dev_plugin.py   Copy installer (validates, backs up, verifies, deploys cron script)
scripts/camofox_cookie_sync.py  Scheduled cookie refresh (no-agent cron script)
scripts/smoke_live.py           Live end-to-end smoke (search/extract/advance) vs a live server
tests/                          Offline unit tests (no network, no daemon required)
```

## Install

```bash
python scripts/install_dev_plugin.py --target-hermes-home "$HERMES_HOME"
hermes plugins enable web/camofox_search
hermes config set web.search_backend camofox-search
hermes config set web.extract_backend camofox-search
```

`--dry-run` previews without writing; `--replace` replaces an existing installed
copy after writing a timestamped backup under `$HERMES_HOME/backups/plugins/web/`.
Then `hermes plugins enable web/camofox_search` (a no-op on an enabled plugin) plus
a control-socket reload so a RUNNING gateway picks up changed tools/skills — new
sessions get them on their next start:

```bash
python -c "from pathlib import Path; from gateway.control_socket import reload_gateway_plugins; print(reload_gateway_plugins(Path(r'$HERMES_HOME')))"
```

A full Gateway restart also works but is unnecessary (and is refused from inside
an agent session). Validate the deployed copy with
`hermes plugins validate "$HERMES_HOME/plugins/web/camofox_search"`.

## Requirements

- A reachable Camofox server: `CAMOFOX_URL` (default `http://localhost:9377`)
- `CAMOFOX_API_KEY` when the server runs with auth enabled
- `requests` (already present in every Hermes runtime)

## Development

```bash
uv venv && uv pip install -p .venv pytest requests
.venv/Scripts/python -m pytest -q     # Windows
.venv/bin/python -m pytest -q         # POSIX
```

Unit tests are fully offline (HTTP is faked). With a Camofox server running,
`python -X utf8 scripts/smoke_live.py` exercises the real search + extract path
through `CamofoxSearchProvider` and cleans up its session.
