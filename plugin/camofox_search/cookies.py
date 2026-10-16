"""Local-browser cookie export → camofox-browser import.

Used by the ``camofox-search-import-cookies`` tool and the scheduled refresh
job: detect the local browsers, export their cookies, and push them into the
camofox-browser profile the plugin browses with (``camofox_user_id()``), so
searches/extractions carry the user's logins and consent/clearance cookies.

Supported sources (Windows-first; the plugin only automates Windows paths):

- Chromium family — Chrome, Edge, Brave, Chromium, Vivaldi. Cookie values are
  AES-256-GCM encrypted with a key that is itself DPAPI-protected in
  ``Local State`` (``os_crypt.encrypted_key``); current builds (verified on
  Edge 155) additionally prefix the plaintext with 32 bytes of binding data,
  which the exporter strips. App-bound values (``v20``) cannot be decrypted
  from outside the browser and are skipped, as are values that fail to
  unprotect. Requires the ``cryptography`` package for the AES-GCM path
  (present in every Hermes runtime).
- Firefox — ``cookies.sqlite`` (``moz_cookies``) stores plaintext values.

The database file is copied (with its WAL/SHM siblings) before reading, so a
running browser is fine. Expired cookies and duplicates are dropped; export
stats report every skip reason. Cookie *values* never appear in tool output —
only counts.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "BrowserProfile", "ExportResult", "ImportResult",
    "CookieSourceLockedError", "CdpError",
    "detect_browsers", "export_cookies", "export_via_cdp", "import_cookies",
    "cdp_reachable", "cdp_page_ws_url", "cdp_command", "cdp_ports",
    "BROWSER_IDS", "MAX_COOKIES_PER_REQUEST",
]

MAX_COOKIES_PER_REQUEST = 500  # camofox-browser caps one import POST at 500
_UNIX_EPOCH_OFFSET = 11644473600  # seconds between 1601-01-01 and 1970-01-01

BROWSER_IDS = ("chrome", "edge", "brave", "chromium", "vivaldi", "firefox")

# Chromium-family browsers share the ``<root>/User Data/<Profile>/Network/Cookies`` shape.
_CHROMIUM_ROOTS: dict[str, tuple[str, ...]] = {
    "chrome": ("Google", "Chrome", "User Data"),
    "edge": ("Microsoft", "Edge", "User Data"),
    "brave": ("BraveSoftware", "Brave-Browser", "User Data"),
    "chromium": ("Chromium", "User Data"),
    "vivaldi": ("Vivaldi", "User Data"),
}


@dataclass(frozen=True)
class BrowserProfile:
    """One detected browser profile with a cookie database."""

    browser: str  # chrome | edge | brave | chromium | vivaldi | firefox
    family: str  # "chromium" | "firefox"
    name: str  # profile directory name, e.g. "Default", "xyz.default-release"
    cookies_path: Path
    user_data_dir: Path  # Chromium user-data root (holds Local State)
    last_modified: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "browser": self.browser,
            "family": self.family,
            "profile": self.name,
            "cookies_path": str(self.cookies_path),
            "last_modified": int(self.last_modified),
            "last_modified_iso": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.last_modified)),
        }


@dataclass
class ExportResult:
    """Outcome of one export; ``cookies`` stays internal to the caller."""

    profile: BrowserProfile
    cookies: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Value-free summary (safe for tool output/logs)."""
        return {**self.profile.to_dict(), "stats": dict(self.stats)}


@dataclass
class ImportResult:
    """Outcome of pushing cookies into a camofox-browser profile."""

    sent: int = 0
    accepted: int = 0  # count echoed by the daemon (browser-side acceptance)
    batches: int = 0
    present_after: Optional[int] = None  # round-trip verification count

    def to_dict(self) -> dict[str, Any]:
        return {"sent": self.sent, "accepted": self.accepted,
                "batches": self.batches, "present_after": self.present_after}


class CookieSourceLockedError(RuntimeError):
    """The cookie database is locked by the running browser.

    Chromium-family browsers hold an exclusive handle on the live ``Cookies``
    database, so no other process can read it until the browser closes.
    """


class CdpError(RuntimeError):
    """A Chrome DevTools Protocol endpoint could not be used."""


def cdp_ports() -> list[int]:
    """CDP ports to probe: ``CAMOFOX_SEARCH_CDP_PORTS`` or the 9222 default."""
    raw = os.environ.get("CAMOFOX_SEARCH_CDP_PORTS", "").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()] or ["9222"]
    ports: list[int] = []
    for part in parts:
        try:
            ports.append(int(part))
        except ValueError:
            continue
    return ports


def cdp_reachable(port: int, *, timeout: float = 2.0) -> bool:
    """True when a devtools HTTP endpoint answers on ``port``."""
    import requests

    try:
        return requests.get(f"http://127.0.0.1:{port}/json/version",
                            timeout=timeout).status_code == 200
    except Exception:  # noqa: BLE001 - probing must never raise
        return False


def cdp_page_ws_url(port: int, *, timeout: float = 2.0) -> Optional[str]:
    """A *page*-level devtools websocket on ``port`` (Network needs a page session).

    Uses the first open page target; if the browser has no page open, a blank
    one is created through ``/json/new``.
    """
    import requests

    try:
        response = requests.get(f"http://127.0.0.1:{port}/json/list",
                                timeout=timeout)
        if response.status_code == 200:
            for target in response.json() or []:
                if isinstance(target, dict) and target.get("type") == "page" \
                        and target.get("webSocketDebuggerUrl"):
                    return str(target["webSocketDebuggerUrl"])
    except Exception:  # noqa: BLE001 - probing must never raise
        return None
    try:
        created = requests.put(f"http://127.0.0.1:{port}/json/new?about:blank",
                               timeout=timeout)
        if created.status_code in (200, 201):
            payload = created.json()
            ws_url = payload.get("webSocketDebuggerUrl") if isinstance(payload, dict) else None
            return str(ws_url) if ws_url else None
    except Exception:  # noqa: BLE001
        return None
    return None


def cdp_command(ws_url: str, method: str, params: Optional[dict] = None,
                *, timeout: float = 15.0) -> dict:
    """Run one CDP command over a short-lived websocket; returns its result.

    Requires the ``websockets`` package (present in every Hermes runtime).
    """
    try:
        from websockets.sync.client import connect
    except ImportError as exc:  # pragma: no cover - runtime dependency
        raise CdpError("the 'websockets' package is required for CDP export") from exc
    import json as _json

    try:
        with connect(ws_url, open_timeout=timeout, close_timeout=2) as ws:
            ws.send(_json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                message = _json.loads(ws.recv(timeout=timeout))
                if message.get("id") != 1:
                    continue  # event/other response — keep waiting for ours
                if "error" in message:
                    raise CdpError(f"{method} failed: {message['error']}")
                return message.get("result") or {}
    except CdpError:
        raise
    except Exception as exc:  # noqa: BLE001 - connection/protocol failure
        raise CdpError(f"{method} over {ws_url} failed: {exc}") from exc


def _normalize_cdp_cookie(raw: dict, *, now: float,
                          suffixes: list[str]) -> Optional[dict]:
    """One CDP ``Network.getAllCookies`` entry → Playwright cookie dict."""
    domain = str(raw.get("domain") or "")
    name = str(raw.get("name") or "")
    if not name or not domain:
        return None
    if suffixes and not any(domain.lower().lstrip(".").endswith(s) for s in suffixes):
        return None
    expires = int(raw.get("expires", -1) or -1)
    if expires > 0 and expires < now:
        return None
    cookie: dict[str, Any] = {
        "name": name, "value": str(raw.get("value") or ""), "domain": domain,
        "path": str(raw.get("path") or "/"), "expires": expires,
        "httpOnly": bool(raw.get("httpOnly")), "secure": bool(raw.get("secure")),
    }
    same_site = raw.get("sameSite")
    if same_site in ("Strict", "Lax", "None") and not (
            same_site == "None" and not cookie["secure"]):
        cookie["sameSite"] = same_site
    return cookie


def export_via_cdp(port: int, *, domains: Optional[Iterable[str]] = None,
                   ws_url: Optional[str] = None) -> ExportResult:
    """Export cookies from a browser exposing CDP on ``port`` (works while running).

    Cookie reads run on a *page* session (the browser session does not expose
    the Network domain on current Chromium), so a blank page is created when
    none is open.
    """
    ws_url = ws_url or cdp_page_ws_url(port)
    if not ws_url:
        raise CdpError(
            f"no CDP page endpoint on 127.0.0.1:{port} (start the browser with "
            f"--remote-debugging-port={port})")
    result = cdp_command(ws_url, "Network.getAllCookies", {})
    rows = result.get("cookies") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise CdpError("Network.getAllCookies returned no cookie list")

    suffixes = [d.strip().lower().lstrip(".") for d in (domains or []) if str(d).strip()]
    now = time.time()
    cookies: list[dict[str, Any]] = []
    filtered = 0
    for raw in rows:
        cookie = _normalize_cdp_cookie(raw, now=now, suffixes=suffixes) \
            if isinstance(raw, dict) else None
        if cookie is None:
            filtered += 1
        else:
            cookies.append(cookie)
    before = len(cookies)
    cookies = _drop_duplicates(cookies)
    profile = BrowserProfile(
        browser="cdp", family="cdp", name=f"127.0.0.1:{port}",
        cookies_path=Path(f"cdp://127.0.0.1:{port}"), user_data_dir=Path(""),
        last_modified=time.time())
    stats = {"rows": len(rows), "exported": len(cookies),
             "expired": 0, "filtered": filtered, "undecryptable": 0,
             "duplicates": before - len(cookies), "key_error": 0}
    return ExportResult(profile=profile, cookies=cookies, stats=stats)


# ---- detection -----------------------------------------------------------


def _chromium_profiles(browser: str, root: Path) -> list[BrowserProfile]:
    found: list[BrowserProfile] = []
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        for candidate in (child / "Network" / "Cookies", child / "Cookies"):
            if candidate.is_file():
                found.append(BrowserProfile(
                    browser=browser, family="chromium", name=child.name,
                    cookies_path=candidate, user_data_dir=root,
                    last_modified=candidate.stat().st_mtime))
                break
    return found


def _firefox_profiles(root: Path) -> list[BrowserProfile]:
    found: list[BrowserProfile] = []
    if not root.is_dir():
        return found
    for child in sorted(root.iterdir()):
        db = child / "cookies.sqlite"
        if child.is_dir() and db.is_file():
            found.append(BrowserProfile(
                browser="firefox", family="firefox", name=child.name,
                cookies_path=db, user_data_dir=root,
                last_modified=db.stat().st_mtime))
    return found


def detect_browsers() -> list[BrowserProfile]:
    """Every local browser profile holding a cookie database, newest first.

    Chromium-family profiles are searched under ``%LOCALAPPDATA%`` and Firefox
    under ``%APPDATA%\\Mozilla\\Firefox\\Profiles``; non-Windows hosts return
    an empty list (paths differ and are not automated here).
    """
    local = os.environ.get("LOCALAPPDATA", "").strip()
    roaming = os.environ.get("APPDATA", "").strip()
    profiles: list[BrowserProfile] = []
    if local:
        for browser, parts in _CHROMIUM_ROOTS.items():
            profiles.extend(_chromium_profiles(browser, Path(local).joinpath(*parts)))
    if roaming:
        profiles.extend(_firefox_profiles(Path(roaming) / "Mozilla" / "Firefox" / "Profiles"))
    profiles.sort(key=lambda p: p.last_modified, reverse=True)
    return profiles


def pick_profile(profiles: Iterable[BrowserProfile], browser: str = "auto",
                 profile: Optional[str] = None) -> BrowserProfile:
    """Choose a detected profile; ``browser='auto'`` prefers the most recent.

    ``profile`` matches the directory name (``Default``, ``Profile 2``,
    a Firefox ``*.default-release`` folder). Raises ``LookupError`` with the
    available choices when nothing matches.
    """
    candidates = list(profiles)
    wanted = (browser or "auto").strip().lower()
    if wanted not in ("", "auto"):
        if wanted not in BROWSER_IDS:
            raise LookupError(
                f"unknown browser {browser!r}; expected auto or one of: "
                + ", ".join(BROWSER_IDS))
        candidates = [p for p in candidates if p.browser == wanted]
    if profile:
        candidates = [p for p in candidates if p.name.lower() == profile.strip().lower()]
    if not candidates:
        available = ", ".join(
            f"{p.browser}/{p.name}" for p in profiles) or "none"
        raise LookupError(
            f"no browser profile matched browser={browser!r} profile={profile!r} "
            f"(detected: {available})")
    return candidates[0]


# ---- Chromium value decryption (Windows) ---------------------------------


class _DpapiError(RuntimeError):
    pass


def _dpapi_unprotect(blob: bytes) -> bytes:
    """Decrypt a DPAPI (user-scope) blob via CryptUnprotectData."""
    if sys.platform != "win32":
        raise _DpapiError("DPAPI is only available on Windows")
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]
    buffer = ctypes.create_string_buffer(blob, len(blob))
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise _DpapiError(f"CryptUnprotectData failed (winerror={ctypes.GetLastError()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def load_chromium_key(user_data_dir: Path) -> bytes:
    """The AES-256-GCM cookie key from ``Local State`` (DPAPI-unwrapped)."""
    state_path = user_data_dir / "Local State"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        wrapped = payload["os_crypt"]["encrypted_key"]
    except (OSError, ValueError, KeyError) as exc:
        raise _DpapiError(f"cannot read the cookie key from {state_path}: {exc}") from exc
    raw = base64.b64decode(wrapped)
    if raw[:5] == b"DPAPI":
        raw = raw[5:]
    key = _dpapi_unprotect(raw)
    if len(key) != 32:
        raise _DpapiError(f"unexpected cookie key length {len(key)}")
    return key


def _decode_chromium_plaintext(plaintext: bytes) -> str:
    """UTF-8 value, tolerating the binding prefix of current Chromium/Edge.

    Chromium/Edge (M131+, verified on Edge 155) prepend 32 bytes of
    storage-binding data to the cookie value before encryption; the value
    itself is the UTF-8 suffix. Older builds store the bare value.
    """
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if len(plaintext) >= 32:
        try:
            return plaintext[32:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _DpapiError(
                f"cookie value is not UTF-8 after the 32-byte prefix: {exc}") from exc
    raise _DpapiError("cookie value is not valid UTF-8")


def decrypt_chromium_value(encrypted: bytes, key: bytes) -> str:
    """Decrypt one Chromium ``encrypted_value`` blob.

    ``v10``/``v11`` → AES-256-GCM with the Local State key (a 32-byte binding
    prefix on the plaintext is stripped when present); app-bound ``v20``
    raises ``_DpapiError`` (undecryptable outside the browser); anything else
    is treated as a raw DPAPI blob.
    """
    if not encrypted:
        return ""
    if encrypted[:3] in (b"v10", b"v11"):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:  # pragma: no cover - runtime dependency
            raise _DpapiError(
                "the 'cryptography' package is required to decrypt Chromium "
                "cookies") from exc
        nonce, payload = encrypted[3:15], encrypted[15:]
        try:
            plaintext = AESGCM(key).decrypt(nonce, payload, None)
        except Exception as exc:  # noqa: BLE001 - invalid tag / bad key
            raise _DpapiError(f"AES-GCM decrypt failed: {exc}") from exc
        return _decode_chromium_plaintext(plaintext)
    if encrypted[:3] == b"v20":
        raise _DpapiError("app-bound (v20) cookie — not decryptable outside the browser")
    return _decode_chromium_plaintext(_dpapi_unprotect(encrypted))


# ---- reading cookie databases --------------------------------------------


def _copy_db(path: Path) -> Path:
    """Copy a SQLite DB (+ WAL/SHM) into a temp dir so a live browser is fine.

    Chromium-family browsers hold an exclusive handle on the live cookie DB:
    when the copy is denied the error is a clear
    :class:`CookieSourceLockedError` (retried once first, in case a write is
    momentarily holding the file).
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="camofox_cookies_"))
    target = tmp_dir / path.name
    for attempt in (0, 1):
        try:
            shutil.copy2(path, target)
            break
        except PermissionError as exc:
            if attempt == 1:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                raise CookieSourceLockedError(
                    f"cannot read the cookie database at {path}: the profile is "
                    f"locked by a running browser ({exc}). Close that browser and "
                    "re-run, or point the export at a browser started with "
                    "--remote-debugging-port=9222 (CDP export works while the "
                    "browser keeps running).") from exc
            time.sleep(1.0)
    for suffix in ("-wal", "-shm"):
        sibling = path.with_name(path.name + suffix)
        if sibling.exists():
            try:
                shutil.copy2(sibling, target.with_name(target.name + suffix))
            except PermissionError:
                pass  # WAL/SHM are best-effort; the main DB copy is authoritative
    return target


def _open_sqlite(path: Path) -> sqlite3.Connection:
    # The DB is our own temp copy, so open read-write: that sidesteps the
    # read-only-WAL restrictions while replaying a snapshot's -wal file.
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _drop_duplicates(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple, dict[str, Any]] = {}
    for cookie in cookies:
        seen[(cookie["domain"], cookie["path"], cookie["name"])] = cookie
    return list(seen.values())


def export_cookies(profile: BrowserProfile, *,
                   domains: Optional[Iterable[str]] = None) -> ExportResult:
    """Export and decrypt one profile's cookies (value-free stats in ``stats``).

    ``domains`` filters by suffix (``"zhihu.com"`` also matches
    ``www.zhihu.com``). Expired cookies, duplicates, undecryptable values and
    rows outside the filter are counted in ``stats``; cookie *values* are
    never logged.
    """
    stats = {"rows": 0, "exported": 0, "expired": 0, "filtered": 0,
             "undecryptable": 0, "duplicates": 0, "key_error": 0}
    copy = _copy_db(profile.cookies_path)
    try:
        conn = _open_sqlite(copy)
        try:
            if profile.family == "firefox":
                rows = conn.execute(
                    "SELECT host, name, value, path, expiry, isSecure, "
                    "isHttpOnly, sameSite FROM moz_cookies").fetchall()
            else:
                rows = conn.execute(
                    "SELECT host_key, name, encrypted_value, path, expires_utc, "
                    "is_secure, is_httponly, samesite FROM cookies").fetchall()
        finally:
            conn.close()
    finally:
        shutil.rmtree(copy.parent, ignore_errors=True)

    stats["rows"] = len(rows)
    suffixes = [d.strip().lower().lstrip(".") for d in (domains or []) if str(d).strip()]
    key: Optional[bytes] = None
    now = time.time()
    cookies: list[dict[str, Any]] = []

    for row in rows:
        if profile.family == "firefox":
            domain, name, value = row["host"], row["name"], row["value"]
            expires = int(row["expiry"] or 0)
            expires = -1 if expires <= 0 else expires
            secure, http_only = bool(row["isSecure"]), bool(row["isHttpOnly"])
            same_site = {0: "None", 1: "Lax", 2: "Strict"}.get(row["sameSite"])
        else:
            domain, name = row["host_key"], row["name"]
            if key is None and not stats["key_error"]:
                try:
                    key = load_chromium_key(profile.user_data_dir)
                except _DpapiError as exc:
                    logger.warning("camofox-search: chromium key unavailable: %s", exc)
                    stats["key_error"] = 1
            if key is None:
                stats["undecryptable"] += 1
                continue
            try:
                value = decrypt_chromium_value(bytes(row["encrypted_value"] or b""), key)
            except _DpapiError:
                stats["undecryptable"] += 1
                continue
            micros = int(row["expires_utc"] or 0)
            expires = -1 if micros <= 0 else (micros // 1_000_000) - _UNIX_EPOCH_OFFSET
            secure, http_only = bool(row["is_secure"]), bool(row["is_httponly"])
            same_site = {0: "None", 1: "Lax", 2: "Strict"}.get(row["samesite"])

        if suffixes and not any(domain.lower().lstrip(".").endswith(suffix)
                                for suffix in suffixes):
            stats["filtered"] += 1
            continue
        if expires > 0 and expires < now:
            stats["expired"] += 1
            continue
        if not name or not domain:
            stats["filtered"] += 1
            continue
        cookie = {
            "name": name, "value": value, "domain": domain,
            "path": str(row["path"] or "/"), "expires": expires,
            "httpOnly": http_only, "secure": secure,
        }
        if same_site and not (same_site == "None" and not secure):
            # Playwright/Chromium reject SameSite=None without Secure.
            cookie["sameSite"] = same_site
        cookies.append(cookie)

    before = len(cookies)
    cookies = _drop_duplicates(cookies)
    stats["duplicates"] = before - len(cookies)
    stats["exported"] = len(cookies)
    return ExportResult(profile=profile, cookies=cookies, stats=stats)


# ---- import into camofox-browser -----------------------------------------


def import_cookies(client: Any, user_id: str, cookies: list[dict[str, Any]],
                   *, tab_id: Optional[str] = None) -> ImportResult:
    """Push cookies into the camofox profile for ``user_id``, batched.

    The daemon caps one request at 500 cookies, so the set is split; every
    batch targets the same tab (``tab_id``) to keep the import unambiguous
    when other sessions exist. Returns sent/batch counts (no values).
    """
    result = ImportResult()
    for start in range(0, len(cookies), MAX_COOKIES_PER_REQUEST):
        batch = cookies[start:start + MAX_COOKIES_PER_REQUEST]
        response = client.set_cookies(user_id, batch, tab_id=tab_id)
        result.batches += 1
        result.sent += len(batch)
        try:
            result.accepted += int(response.get("count", len(batch)))
        except (AttributeError, TypeError, ValueError):
            result.accepted += len(batch)
    return result
