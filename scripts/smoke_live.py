#!/usr/bin/env python3
"""Live end-to-end smoke: CamofoxSearchProvider + the advance tool, live server.

Run with a Python that can import the Hermes runtime (the Hermes venv python),
while a camofox-browser server is reachable:

    python -X utf8 scripts/smoke_live.py [query]

Exercises the rule store, then the real search + extract path, then the
``camofox-search-advance`` tool (URL build, live run, no-extractor guidance),
and cleans up its own sessions. Never prints credentials; exits non-zero on
failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugin.camofox_search.provider import CamofoxSearchProvider  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    query = args[0].strip() if args and args[0].strip() else "hermes agent"

    provider = CamofoxSearchProvider()
    print(f"provider={provider.name} available={provider.is_available()}")
    if not provider.is_available():
        print("FAIL: CAMOFOX_URL / CAMOFOX_API_KEY not configured", file=sys.stderr)
        return 2

    ok = True

    print("--- rule store ---")
    try:
        from plugin.camofox_search import rules as rule_store

        db = rule_store.ensure_db()
        counts = rule_store.count_rules()
        print(f"db: {db}")
        print(f"search_rules={counts['search_rules']} content_rules={counts['content_rules']}")
    except Exception as exc:
        print(f"FAIL rule store: {exc}", file=sys.stderr)
        ok = False

    print(f"--- search({query!r}, limit=5) ---")
    result = provider.search(query, limit=5)
    if not result.get("success"):
        print(f"FAIL search: {result.get('error')}", file=sys.stderr)
        ok = False
    else:
        rows = result["data"]["web"]
        print(f"search returned {len(rows)} result(s)")
        for row in rows[:5]:
            title = row.get("title", "")
            print(f"  {row.get('position')}. {title[:70]} | {row.get('url')}")
        if not rows:
            print("FAIL search: zero results", file=sys.stderr)
            ok = False

    print("--- extract(['https://example.com/']) ---")
    extraction = provider.extract(["https://example.com/"])
    if not extraction.get("success"):
        print(f"FAIL extract: {extraction.get('error')}", file=sys.stderr)
        ok = False
    else:
        entry = extraction["data"][0]
        content = entry.get("content") or ""
        print(f"extract ok: title={entry.get('title')!r} chars={len(content)}")
        if not content:
            print("FAIL extract: empty content", file=sys.stderr)
            ok = False

    print(f"--- camofox-search-advance ---")
    try:
        from plugin.camofox_search.advance import handle_advance

        url_payload = json.loads(handle_advance(
            {"query": query, "engine": "bing-cn", "mode": "url",
             "time": "week", "limit": 5}))
        if not url_payload.get("success"):
            print(f"FAIL advance url mode: {url_payload.get('error')}", file=sys.stderr)
            ok = False
        else:
            print(f"url mode: {url_payload['url']}")
            if "filters=ex1%3A%22ez2%22" not in url_payload["url"]:
                print("FAIL advance url mode: time mapping missing", file=sys.stderr)
                ok = False

        run_payload = json.loads(handle_advance(
            {"query": query, "engine": "bing-cn", "time": "week", "limit": 5}))
        if not run_payload.get("success") or not run_payload.get("executed"):
            print("FAIL advance run: "
                  f"{run_payload.get('error') or run_payload.get('reason')}",
                  file=sys.stderr)
            ok = False
        else:
            rows = run_payload.get("results") or []
            print(f"advance run: {len(rows)} result(s) via {run_payload.get('extractor')}")
            for row in rows[:3]:
                print(f"  {row.get('position')}. {row.get('title', '')[:70]} | {row.get('url', '')}")
            if not rows:
                print("FAIL advance run: zero results", file=sys.stderr)
                ok = False

        no_engine_payload = json.loads(handle_advance(
            {"query": "machine learning", "engine": "baidu", "limit": 3}))
        if no_engine_payload.get("extractor") is None:
            if (no_engine_payload.get("executed") is not False
                    or "camofox-search-manage" not in (no_engine_payload.get("reason") or "")):
                print("FAIL advance no-extractor guidance", file=sys.stderr)
                ok = False
            else:
                print("advance no-extractor path: executed=False + guidance OK")
        else:
            print(f"advance baidu rule present ({no_engine_payload['extractor']}); "
                  f"executed={no_engine_payload.get('executed')}")
    except Exception as exc:  # noqa: BLE001 - smoke reports, never raises
        print(f"FAIL advance: {type(exc).__name__}: {exc}", file=sys.stderr)
        ok = False

    print("SMOKE:" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
