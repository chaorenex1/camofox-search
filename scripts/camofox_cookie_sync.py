#!/usr/bin/env python3
"""Scheduled camofox-browser cookie refresh (Hermes cron: every 72h).

Detects the local browser profile with the most recently used cookie database
(Chromium family or Firefox), exports and decrypts its cookies, imports them
into the Camofox profile ``camofox-search`` browses with, and verifies the
round trip. Prints a one-line, value-free summary; failures print
``FAILED: ...`` and exit non-zero so the cron history shows them.

Run under the Hermes runtime python (it imports the deployed plugin, which
imports ``agent.*``). Deployed alongside the plugin at
``$HERMES_HOME/scripts/camofox_cookie_sync.py``; the cron job references it by
that name.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _hermes_home() -> Path:
    """The Hermes home this script belongs to (script path first, env second)."""
    try:
        candidate = Path(__file__).resolve().parents[1]
        if (candidate / "plugins").is_dir():
            return candidate
    except OSError:
        pass
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    return Path(local) / "hermes" if local else Path.home() / ".hermes"


def _load_plugin():
    """Import the deployed plugin package (``<home>/plugins/web/camofox_search``)."""
    override = os.environ.get("CAMOFOX_SEARCH_PLUGIN_DIR", "").strip()
    plugins_dir = Path(override) if override else _hermes_home() / "plugins" / "web"
    if not (plugins_dir / "camofox_search" / "__init__.py").is_file():
        raise RuntimeError(f"deployed plugin not found under {plugins_dir}")
    sys.path.insert(0, str(plugins_dir))
    import camofox_search.import_cookies as tool  # noqa: PLC0415
    return tool


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    browser = args[0].strip() if args and args[0].strip() else "auto"
    try:
        tool = _load_plugin()
        payload = json.loads(tool.handle_import_cookies(
            {"action": "run", "browser": browser}))
    except Exception as exc:  # noqa: BLE001 - cron scripts report, never raise
        print(f"FAILED: camofox cookie sync raised {type(exc).__name__}: {exc}",
              file=sys.stderr)
        print(f"FAILED: camofox cookie sync raised {type(exc).__name__}: {exc}")
        return 1

    if not payload.get("success"):
        print(f"FAILED: camofox cookie sync: {payload.get('error')}")
        return 1
    if payload.get("imported") is False:
        note = payload.get("note", "nothing imported")
        print(f"WARN: camofox cookie sync: {note} "
              f"stats={json.dumps(payload.get('export', {}).get('stats', {}))}")
        return 0
    if "import" not in payload:
        print(f"FAILED: camofox cookie sync: unexpected payload "
              f"{json.dumps(payload, ensure_ascii=False)[:300]}")
        return 1

    imported = payload.get("import") or {}
    summary = (
        f"camofox cookie sync: browser={payload.get('browser')} "
        f"profile={payload.get('profile')} "
        f"exported={payload.get('export', {}).get('stats', {}).get('exported')} "
        f"accepted={imported.get('accepted')} batches={imported.get('batches')} "
        f"present_after={imported.get('present_after')} "
        f"verified={payload.get('verified')} user={payload.get('user_id')}"
    )
    print(summary)
    skipped = {k: v for k, v in (payload.get("export", {}).get("stats") or {}).items()
               if v and k in ("undecryptable", "expired", "filtered", "key_error")}
    if skipped:
        print(f"camofox cookie sync skipped: {json.dumps(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
