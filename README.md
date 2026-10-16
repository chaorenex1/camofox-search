# Hermes camofox-search plugin

A Hermes web-search provider plugin that services `web_search` and `web_extract`
through a self-hosted [Camofox](https://github.com/jo-inc/camofox-browser)
anti-detection browser (Camoufox engine) instead of a hosted search API.

- Provider name: `camofox-search` (select via `web.search_backend` / `web.extract_backend`)
- Plugin key: `web/camofox_search`
- No API keys of its own: it uses the local Camofox server (`CAMOFOX_URL`, `CAMOFOX_API_KEY`)

## How it works

Each `search()` call opens one throwaway Camofox session (a unique `userId`),
navigates it to the selected engine's result page, extracts organic results from
the page DOM via the Camofox `/evaluate` endpoint, and tears the session down in
a `finally` block. `extract()` does the same per URL and returns readable page
text. Plugin activity therefore never leaks into the browser sessions Hermes'
own `browser_*` tools own.

Search engines: `bing` (default), `google`, `duckduckgo` — select with
`CAMOFOX_SEARCH_ENGINE`. The Bing extractor is verified against `cn.bing.com`;
`google`/`duckduckgo` extractors are best-effort.

## Layout

```
plugin/camofox_search/          Plugin source (install target: $HERMES_HOME/plugins/web/camofox_search)
  plugin.yaml                   Manifest: kind=backend, provides_web_providers
  __init__.py                   register(ctx) entry point
  provider.py                   CamofoxSearchProvider (WebSearchProvider)
  client.py                     Camofox REST client (tab lifecycle, Bearer auth)
  engines.py                    Engine URL builders + page-context extraction JS
  settings.py                   Env resolution (secret scope -> process env -> Hermes .env)
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
