#!/usr/bin/env python3
"""Live end-to-end smoke: CamofoxSearchProvider against a running Camofox server.

Run with a Python that can import the Hermes runtime (the Hermes venv python),
while a camofox-browser server is reachable:

    python -X utf8 scripts/smoke_live.py [query]

Exercises the rule store, then the real search + extract path, and cleans up
its own sessions. Never prints credentials; exits non-zero on failure.
"""

from __future__ import annotations

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

    print("SMOKE:" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
