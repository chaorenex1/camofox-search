# Hermes camofox-search plugin

A Hermes web-search provider plugin that services `web_search` and `web_extract`
through a self-hosted [Camofox](https://github.com/jo-inc/camofox-browser)
anti-detection browser (Camoufox engine) instead of a hosted search API.

- Provider name: `camofox-search` (select via `web.search_backend` / `web.extract_backend`)
- Plugin key: `web/camofox_search`
- No API keys of its own: it uses the local Camofox server (`CAMOFOX_URL`, `CAMOFOX_API_KEY`)
- Rule-driven: search engines and page extraction are defined in a SQLite
  database, editable at runtime via the bundled `camofox-search-manage` tool

## How it works

Each `search()` call opens one throwaway Camofox session (a unique `userId`),
navigates it to the selected engine's result page, extracts organic results from
the page DOM via the Camofox `/evaluate` endpoint, and tears the session down in
a `finally` block. `extract()` does the same per URL and returns readable page
text. Plugin activity therefore never leaks into the browser sessions Hermes'
own `browser_*` tools own.

Engine selection is rule-driven: the rule database is consulted first — a rule
matching the configured engine resolves the SERP URL template and extraction JS
— with the built-in engines (`bing` default, `google`, `duckduckgo`) as the
fallback. `CAMOFOX_SEARCH_ENGINE` selects by engine id or engine domain
(`bing`, `bing.com` and `cn.bing.com` all resolve to the same rule; longest
domain wins). The Bing extractor is verified against `cn.bing.com`;
`google`/`duckduckgo` extractors are best-effort.

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
  rule for `yandex.com`, point `CAMOFOX_SEARCH_ENGINE` at `yandex`, done.
- **Content rule** — `extract_js` returns
  `JSON.stringify({ok:true,title,url,text})` or a plain string. Matches the
  domain and every subdomain (longest match wins); an unusable rule result
  falls back to the default full-page text extraction, and the result metadata
  records the path (`metadata.extract_rule`, plus `extract_rule_attempted` on
  fallback).
- The built-in engines are seeded into `search_rules` on first database
  creation (`INSERT OR IGNORE` — edits are never overwritten), so the table
  doubles as documentation of the default behavior.
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
author and store a rule, verify it, iterate.

## Layout

```
plugin/camofox_search/          Plugin source (install target: $HERMES_HOME/plugins/web/camofox_search)
  plugin.yaml                   Manifest: kind=backend, provides_web_providers, provides_tools
  __init__.py                   register(ctx): provider + tool + bundled skill
  provider.py                   CamofoxSearchProvider (rule-aware search/extract)
  client.py                     Camofox REST client (tab lifecycle, Bearer auth)
  engines.py                    Built-in engines as data: domain + URL/JS templates
  rules.py                      SQLite rule store ($HERMES_HOME/camofox-search/, WAL)
  manage.py                     camofox-search-manage tool (rules CRUD)
  settings.py                   Env resolution (secret scope -> process env -> Hermes .env)
  skills/camofox-search-rule/   Bundled skill (SKILL.md)
scripts/install_dev_plugin.py   Copy installer (validates, backs up, verifies hashes)
scripts/smoke_live.py           Live end-to-end smoke against a running Camofox server
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
Restart the Hermes Gateway/Desktop afterwards so the copied plugin is loaded.

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
