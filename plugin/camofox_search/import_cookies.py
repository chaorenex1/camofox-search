"""Agent-facing tool: ``camofox-search-import-cookies``.

Exports cookies from a local browser profile (Chromium family or Firefox, see
:mod:`cookies`) and imports them into the Camofox profile the plugin browses
with (``camofox_user_id()``), so searches and extractions carry the user's
logins / consent / clearance cookies. The same logic backs the scheduled
refresh job (``$HERMES_HOME/scripts/camofox_cookie_sync.py``).

Actions:

- ``detect`` — list every detected browser profile with a cookie database
  (browser, profile name, path, last use).
- ``run`` — export (decrypt) + import + round-trip verification on a scratch
  tab. ``dry_run: true`` stops after the export and reports stats only.

Cookie *values* never appear in the output — only counts and reasons.

Handler contract: returns a JSON *string*. Envelope:
``{"success": true, ...}`` or ``{"success": false, "error": "..."}``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .client import CamofoxClient, CamofoxError
from .cookies import (
    BROWSER_IDS,
    CdpError,
    CookieSourceLockedError,
    cdp_ports,
    cdp_reachable,
    detect_browsers,
    export_cookies,
    export_via_cdp,
    import_cookies,
    pick_profile,
)
from .provider import open_page_tab
from .settings import camofox_user_id

logger = logging.getLogger(__name__)

TOOL_NAME = "camofox-search-import-cookies"

_ACTIONS = ("detect", "run")

_HINT = ("Refresh cadence: the scheduled job runs this every 3 days; call it "
         "manually after logging into new sites in the local browser. "
         "Cookie values are never printed.")


class ImportCookiesError(ValueError):
    """A tool argument failed validation."""


IMPORT_COOKIES_SCHEMA: dict[str, Any] = {
    "name": TOOL_NAME,
    "description": (
        "Import cookies from the user's LOCAL browser (Chrome/Edge/Brave/"
        "Chromium/Vivaldi or Firefox profile on Windows) into the Camofox "
        "browser profile that camofox-search browses with, so web_search / "
        "web_extract / camofox-search-advance run with the user's logins and "
        "clearance cookies. action='detect' lists the detected browser "
        "profiles and their cookie databases; action='run' exports+decrypts "
        "the chosen profile's cookies and imports them (batched; the daemon "
        "caps 500 cookies per request), then verifies the round-trip on a "
        "scratch tab. Filters: browser (auto|chrome|edge|brave|chromium|"
        "vivaldi|firefox), profile (directory name), domains (suffix filter), "
        "user_id (camofox profile to target, default: the plugin's). "
        "dry_run=true exports only. App-Bound v20 cookies (modern Edge/Chrome) "
        "export through the optional rookie-cookies package while the browser "
        "is closed; without it they are skipped. A running Chromium-family browser locks "
        "its cookie DB: the tool then falls back to a CDP export on "
        "127.0.0.1:9222 (or CAMOFOX_SEARCH_CDP_PORTS) when such a browser is "
        "reachable, and otherwise reports exactly what to close or re-run. "
        "Only counts and skip reasons are "
        "returned — cookie values are never printed. " + _HINT
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": "'detect' lists local browser profiles; 'run' exports and imports cookies.",
            },
            "browser": {
                "type": "string",
                "enum": ["auto", *BROWSER_IDS],
                "description": "Source browser; 'auto' (default) picks the profile with the most recently used cookie database.",
            },
            "profile": {
                "type": "string",
                "description": "Profile directory name (e.g. 'Default', 'Profile 2', a Firefox '*.default-release'); defaults to the most recent matching profile.",
            },
            "domains": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "Only export cookies whose domain ends with one of these suffixes (e.g. 'zhihu.com', 'bilibili.com'). Omit for all cookies.",
            },
            "user_id": {
                "type": "string",
                "description": "Camofox user/profile to import into. Defaults to the plugin's profile (CAMOFOX_SEARCH_USER_ID / 'camofox_search') — change it only for deliberate isolation.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "With action='run': export and report stats without importing.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _ok(**payload: Any) -> str:
    return json.dumps({"success": True, **payload}, ensure_ascii=False)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"success": False, "error": message, **extra},
                      ensure_ascii=False)


def _string_arg(args: dict[str, Any], name: str) -> Optional[str]:
    if name not in args or args.get(name) is None:
        return None
    value = args.get(name)
    if not isinstance(value, str):
        raise ImportCookiesError(f"{name} must be a string")
    return value


def _domain_list(args: dict[str, Any]) -> list[str]:
    value = args.get("domains")
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise ImportCookiesError("domains must be a string or a list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ImportCookiesError("domains entries must be strings")
        if item.strip():
            out.append(item.strip())
    return out


def handle_import_cookies(args: dict[str, Any], **_kwargs: Any) -> str:
    """Tool handler: detect local browser profiles or run the cookie import."""
    args = args if isinstance(args, dict) else {}
    try:
        return _ok(**_dispatch(args))
    except (ImportCookiesError, LookupError) as exc:
        return _err(str(exc))
    except CamofoxError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001 - tool handlers must never raise
        return _err(f"{type(exc).__name__}: {exc}")


def _dispatch(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        raise ImportCookiesError(
            f"invalid action {action!r}; expected one of: {', '.join(_ACTIONS)}")

    browser = (_string_arg(args, "browser") or "auto").strip() or "auto"
    profile = _string_arg(args, "profile")

    if action == "detect":
        profiles = detect_browsers()
        cdp = [{"port": port, "reachable": bool(cdp_reachable(port))}
               for port in cdp_ports()]
        return {
            "action": "detect",
            "count": len(profiles),
            "browsers": [entry.to_dict() for entry in profiles],
            "cdp": cdp,
            "hint": ("Pass browser/profile to action='run' to import a specific "
                     "profile; 'auto' uses the most recently used one. A running "
                     "Chromium-family browser locks its cookie file — a reachable "
                     "CDP endpoint (cdp.reachable) is used as the fallback. "
                     + _HINT),
        }

    domains = _domain_list(args)
    profiles = detect_browsers()
    chosen = pick_profile(profiles, browser, profile)
    source = "file"
    try:
        export = export_cookies(chosen, domains=domains)
    except CookieSourceLockedError as locked:
        export = None
        attempts: list[str] = []
        for port in cdp_ports():
            try:
                export = export_via_cdp(port, domains=domains)
                source = "cdp"
                break
            except CdpError as cdp_exc:
                attempts.append(f"127.0.0.1:{port}: {cdp_exc}")
        if export is None:
            raise ImportCookiesError(
                f"{locked} CDP fallback unavailable "
                f"({' ; '.join(attempts) or 'no ports configured'}).")

    payload: dict[str, Any] = {
        "action": "run",
        "source": source,
        "browser": chosen.browser if source == "file" else export.profile.browser,
        "profile": chosen.name if source == "file" else export.profile.name,
        "export": export.summary(),
    }
    if args.get("dry_run"):
        payload["dry_run"] = True
        return payload
    if not export.cookies:
        payload.update({
            "imported": False,
            "note": ("nothing to import (all rows filtered, expired or "
                     "undecryptable) — check export.stats"),
        })
        return payload

    user_id = (_string_arg(args, "user_id") or "").strip() or camofox_user_id()
    client = CamofoxClient()
    tab_id: Optional[str] = None
    try:
        tab_id = open_page_tab(client, user_id, "")
        imported = import_cookies(client, user_id, export.cookies, tab_id=tab_id)
        try:
            present = client.get_tab_cookies(tab_id, user_id)
            imported.present_after = len(present)
        except CamofoxError as exc:
            logger.debug("cookie round-trip verification failed: %s", exc)
    finally:
        if tab_id:
            try:
                client.close_tab(tab_id, user_id)
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.debug("import tab cleanup failed for %s: %s", tab_id, exc)

    payload["user_id"] = user_id
    payload["import"] = imported.to_dict()
    if imported.present_after is not None:
        payload["verified"] = imported.present_after >= imported.accepted
        if not payload["verified"]:
            payload["note"] = (
                "round-trip count is lower than the accepted count — some "
                "cookies may have been merged by the browser; re-run with a "
                "single cookie set if this looks wrong")
    payload["hint"] = _HINT
    return payload
