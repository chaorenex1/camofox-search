"""Offline tests for cookies.py — detection, export/decryption, import batching."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from plugin.camofox_search import cookies as cookies_module
from plugin.camofox_search.cookies import (
    BrowserProfile,
    detect_browsers,
    export_cookies,
    import_cookies,
    pick_profile,
)

KEY = b"k" * 32


def _aesgcm():
    return pytest.importorskip("cryptography.hazmat.primitives.ciphers.aead")


def _encrypt_v10(key: bytes, value: str) -> bytes:
    AESGCM = _aesgcm().AESGCM
    nonce = os.urandom(12)
    return b"v10" + nonce + AESGCM(key).encrypt(nonce, value.encode("utf-8"), None)


def _make_firefox_profile(root: Path, rows: list[tuple]) -> BrowserProfile:
    profile_dir = root / "abc123.default-release"
    profile_dir.mkdir(parents=True, exist_ok=True)
    db = profile_dir / "cookies.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT, path TEXT, "
        "expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER, sameSite INTEGER)")
    conn.executemany("INSERT INTO moz_cookies VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return BrowserProfile(browser="firefox", family="firefox",
                          name=profile_dir.name, cookies_path=db,
                          user_data_dir=root,
                          last_modified=db.stat().st_mtime)


def _make_chromium_profile(root: Path, rows: list[tuple],
                           browser: str = "edge") -> BrowserProfile:
    profile_dir = root / "Default" / "Network"
    profile_dir.mkdir(parents=True, exist_ok=True)
    db = profile_dir / "Cookies"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB, "
        "path TEXT, expires_utc INTEGER, is_secure INTEGER, is_httponly INTEGER, "
        "samesite INTEGER)")
    conn.executemany("INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return BrowserProfile(browser=browser, family="chromium", name="Default",
                          cookies_path=db, user_data_dir=root,
                          last_modified=db.stat().st_mtime)


@pytest.fixture(autouse=True)
def _rookie_opt_out(monkeypatch):
    """Default every test to the built-in sqlite path (no real rookie reads).

    Tests that exercise the rookie path re-patch ``_load_rookie`` afterwards;
    without this default, a locally installed rookie-cookies would reach the
    REAL browser stores on this machine.
    """
    monkeypatch.setattr(cookies_module, "_load_rookie", lambda: None)


# ---- detection -----------------------------------------------------------


def test_detect_browsers_finds_chromium_and_firefox_profiles(tmp_path, monkeypatch):
    local = tmp_path / "LocalAppData"
    roaming = tmp_path / "Roaming"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))

    old = _make_chromium_profile(local / "Microsoft" / "Edge" / "User Data",
                                 [], browser="edge")
    new = _make_chromium_profile(local / "Google" / "Chrome" / "User Data",
                                 [], browser="chrome")
    os.utime(new.cookies_path, (time.time() + 10, time.time() + 10))
    ff = _make_firefox_profile(roaming / "Mozilla" / "Firefox" / "Profiles", [])

    detected = detect_browsers()
    names = [(p.browser, p.name) for p in detected]
    assert ("chrome", "Default") in names
    assert ("edge", "Default") in names
    assert ("firefox", ff.name) in names
    # newest-first ordering
    assert detected[0].browser == "chrome"
    row = detected[0].to_dict()
    assert {"browser", "family", "profile", "cookies_path",
            "last_modified", "last_modified_iso"} <= set(row)
    assert old.last_modified > 0


def test_pick_profile_filters_and_reports_choices(tmp_path):
    older = BrowserProfile("edge", "chromium", "Default",
                           tmp_path / "a", tmp_path, 100.0)
    newer = BrowserProfile("chrome", "chromium", "Profile 2",
                           tmp_path / "b", tmp_path, 200.0)
    profiles = [newer, older]

    assert pick_profile(profiles, "auto").browser == "chrome"  # most recent
    assert pick_profile(profiles, "edge").name == "Default"
    assert pick_profile(profiles, "auto", "profile 2").browser == "chrome"
    with pytest.raises(LookupError) as excinfo:
        pick_profile(profiles, "vivaldi")
    assert "chrome/Profile 2" in str(excinfo.value)
    with pytest.raises(LookupError):
        pick_profile(profiles, "auto", "nope")


# ---- firefox export ------------------------------------------------------


def test_export_firefox_maps_fields_filters_and_dedupes(tmp_path):
    now = int(time.time())
    profile = _make_firefox_profile(tmp_path, [
        # host, name, value, path, expiry, isSecure, isHttpOnly, sameSite
        (".example.com", "valid", "v1", "/", now + 3600, 1, 1, 1),
        (".example.com", "session", "v2", "/", 0, 0, 0, 0),
        (".example.com", "expired", "v3", "/", now - 10, 0, 0, 0),
        (".other.org", "elsewhere", "v4", "/", now + 3600, 0, 0, 2),
        (".example.com", "valid", "v5", "/", now + 3600, 1, 1, 1),  # duplicate
    ])
    result = export_cookies(profile)
    by_name = {c["name"]: c for c in result.cookies}

    assert set(by_name) == {"valid", "session", "elsewhere"}
    assert by_name["valid"]["sameSite"] == "Lax"
    assert by_name["valid"]["httpOnly"] is True and by_name["valid"]["secure"] is True
    assert by_name["session"]["expires"] == -1
    assert "sameSite" not in by_name["session"]  # None + insecure → dropped
    assert result.stats == {"rows": 5, "exported": 3, "expired": 1,
                            "filtered": 0, "undecryptable": 0,
                            "duplicates": 1, "key_error": 0}

    filtered = export_cookies(profile, domains=["other.org"])
    assert [c["name"] for c in filtered.cookies] == ["elsewhere"]
    assert filtered.stats["exported"] == 1
    assert filtered.stats["filtered"] == 4  # every non-matching row, incl. expired


# ---- chromium export / decryption ---------------------------------------


def test_export_chromium_decrypts_v10_skips_v20(tmp_path, monkeypatch):
    _aesgcm()  # cryptography required for the AES-GCM path
    now = int(time.time())
    micros = lambda seconds: (seconds + 11644473600) * 1_000_000  # noqa: E731
    profile = _make_chromium_profile(tmp_path, [
        (".example.com", "good", _encrypt_v10(KEY, "secret"), "/",
         micros(now + 3600), 1, 1, 1),
        (".example.com", "appbound", b"v20" + b"x" * 40, "/",
         micros(now + 3600), 1, 0, -1),
        (".example.com", "gone", _encrypt_v10(KEY, "old"), "/",
         micros(now - 100), 1, 0, -1),
    ])
    monkeypatch.setattr(cookies_module, "load_chromium_key", lambda _dir: KEY)

    result = export_cookies(profile)

    assert [c["name"] for c in result.cookies] == ["good"]
    assert result.cookies[0]["value"] == "secret"
    assert result.cookies[0]["sameSite"] == "Lax"
    assert result.stats["undecryptable"] == 1
    assert result.stats["expired"] == 1
    assert result.stats["key_error"] == 0


def test_export_chromium_key_error_is_counted(tmp_path, monkeypatch):
    def _boom(_dir):
        raise cookies_module._DpapiError("no Local State")

    profile = _make_chromium_profile(tmp_path, [
        (".example.com", "a", b"v10" + b"x" * 40, "/", 0, 1, 0, -1),
        (".example.com", "b", b"v10" + b"y" * 40, "/", 0, 1, 0, -1),
    ])
    monkeypatch.setattr(cookies_module, "load_chromium_key", _boom)

    result = export_cookies(profile)
    assert result.cookies == []
    assert result.stats["exported"] == 0
    assert result.stats["undecryptable"] == 2
    assert result.stats["key_error"] == 1


# ---- chromium export via rookie-cookies -----------------------------------


class _FakeWarning:
    def __init__(self, code: str, count: int):
        self.code = code
        self.count = count


class _FakeSnapshot:
    def __init__(self, rows: list[dict], warnings: list | None = None):
        self._rows = rows
        self.warnings = warnings or []

    def as_list(self) -> list[dict]:
        return list(self._rows)


class _FakeRookie:
    """Pipe double for the rookie_cookies module (never touches a browser)."""

    def __init__(self, rows: list[dict], warnings: list | None = None,
                 error: Exception | None = None):
        self.rows = rows
        self.warnings = warnings
        self.error = error
        self.calls: list[dict] = []

    def read(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _FakeSnapshot(self.rows, self.warnings)


def _rookie_row(domain: str = ".example.com", name: str = "good",
                value: str = "secret", path: str = "/", expires=None,
                secure: bool = True, http_only: bool = True,
                same_site: int = 1) -> dict:
    return {"domain": domain, "name": name, "value": value, "path": path,
            "expires": expires, "secure": secure, "http_only": http_only,
            "same_site": same_site}


def test_export_chromium_uses_rookie_and_maps_rows(tmp_path, monkeypatch):
    now = int(time.time())
    fake = _FakeRookie(rows=[
        _rookie_row(expires=now + 3600),                                   # keep
        _rookie_row(expires=now + 3600),                                   # duplicate
        _rookie_row(name="session", expires=None, same_site=-1,
                    secure=False, http_only=False),                        # keep, session
        _rookie_row(name="note", same_site=0, secure=False),               # keep; None + insecure → sameSite dropped
        _rookie_row(name="old", expires=now - 100),                        # expired
        _rookie_row(name="other", domain=".other.org", expires=now + 60),  # keep
    ], warnings=[_FakeWarning("invalid_octets", 2)])
    monkeypatch.setattr(cookies_module, "_load_rookie", lambda: fake)
    profile = _make_chromium_profile(tmp_path, [])

    result = export_cookies(profile)

    by_name = {c["name"]: c for c in result.cookies}
    assert set(by_name) == {"good", "session", "note", "other"}
    assert by_name["good"]["value"] == "secret"
    assert by_name["good"]["httpOnly"] is True
    assert by_name["good"]["sameSite"] == "Lax"
    assert by_name["session"]["expires"] == -1  # None → session cookie
    assert "sameSite" not in by_name["session"]  # unspecified (enum -1) → no attribute
    assert "sameSite" not in by_name["note"]  # None + insecure → attribute dropped
    assert result.stats == {"rows": 8, "exported": 4, "expired": 1,
                            "filtered": 0, "undecryptable": 2,
                            "duplicates": 1, "key_error": 0}
    call = fake.calls[0]
    assert call["browser"] == "edge" and call["profile"] == "Default"
    assert call["include_expired"] is True
    assert call["app_bound"] == "injection_only"

    filtered = export_cookies(profile, domains=["other.org"])
    assert [c["name"] for c in filtered.cookies] == ["other"]
    assert filtered.stats["filtered"] == 5  # every non-matching row, incl. duplicate and expired


def test_export_chromium_rookie_failure_falls_back_to_sqlite(tmp_path, monkeypatch):
    _aesgcm()
    now = int(time.time())
    micros = lambda seconds: (seconds + 11644473600) * 1_000_000  # noqa: E731
    profile = _make_chromium_profile(tmp_path, [
        (".example.com", "good", _encrypt_v10(KEY, "secret"), "/",
         micros(now + 3600), 1, 1, 1)])
    fake = _FakeRookie(rows=[], error=RuntimeError("share-locked (os error 32)"))
    monkeypatch.setattr(cookies_module, "_load_rookie", lambda: fake)
    monkeypatch.setattr(cookies_module, "load_chromium_key", lambda _dir: KEY)

    result = export_cookies(profile)

    assert fake.calls  # the rookie attempt happened before the fallback
    assert [c["name"] for c in result.cookies] == ["good"]
    assert result.cookies[0]["value"] == "secret"
    assert result.stats["exported"] == 1 and result.stats["key_error"] == 0


def test_export_firefox_never_uses_rookie(tmp_path, monkeypatch):
    fake = _FakeRookie(rows=[])
    monkeypatch.setattr(cookies_module, "_load_rookie", lambda: fake)
    profile = _make_firefox_profile(tmp_path, [
        (".example.com", "a", "v", "/", 0, 0, 0, 0)])

    result = export_cookies(profile)

    assert fake.calls == []
    assert result.stats["exported"] == 1


def test_app_bound_policy_env(monkeypatch):
    monkeypatch.delenv("CAMOFOX_SEARCH_COOKIE_APP_BOUND", raising=False)
    assert cookies_module._app_bound_policy() == "injection_only"
    monkeypatch.setenv("CAMOFOX_SEARCH_COOKIE_APP_BOUND", "disabled")
    assert cookies_module._app_bound_policy() == "disabled"
    monkeypatch.setenv("CAMOFOX_SEARCH_COOKIE_APP_BOUND", "allow_elevated_fallback")
    assert cookies_module._app_bound_policy() == "allow_elevated_fallback"
    monkeypatch.setenv("CAMOFOX_SEARCH_COOKIE_APP_BOUND", "bogus")
    assert cookies_module._app_bound_policy() == "injection_only"


def test_decrypt_strips_32_byte_binding_prefix():
    _aesgcm()
    AESGCM = _aesgcm().AESGCM
    prefix = b"\xff" * 32  # never valid UTF-8
    nonce = os.urandom(12)
    blob = b"v10" + nonce + AESGCM(KEY).encrypt(nonce, prefix + b"stored-value", None)
    assert cookies_module.decrypt_chromium_value(blob, KEY) == "stored-value"

    # a bare value (older builds) is untouched
    nonce2 = os.urandom(12)
    bare = b"v10" + nonce2 + AESGCM(KEY).encrypt(nonce2, b"plain", None)
    assert cookies_module.decrypt_chromium_value(bare, KEY) == "plain"


def test_decrypt_legacy_dpapi_blob(monkeypatch):
    monkeypatch.setattr(cookies_module, "_dpapi_unprotect",
                        lambda blob: b"legacy-value")
    assert cookies_module.decrypt_chromium_value(b"\x01\x00\x00\x00raw", KEY) == \
        "legacy-value"
    with pytest.raises(cookies_module._DpapiError):
        cookies_module.decrypt_chromium_value(b"v20nope", KEY)


def test_summary_never_carries_cookie_values(tmp_path):
    profile = _make_firefox_profile(tmp_path, [
        (".example.com", "token", "super-secret-value", "/", 0, 1, 1, 1)])
    summary = json.dumps(export_cookies(profile).summary())
    assert "super-secret-value" not in summary
    assert "value" not in summary.replace("cookies_path", "")


# ---- locked database + CDP ------------------------------------------------


def test_copy_db_raises_locked_after_retry(tmp_path, monkeypatch):
    profile = _make_firefox_profile(tmp_path, [])
    (tmp_path / "noop").write_text("x")

    def _deny(*args, **kwargs):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(cookies_module.shutil, "copy2", _deny)
    monkeypatch.setattr(cookies_module.time, "sleep", lambda _s: None)
    with pytest.raises(cookies_module.CookieSourceLockedError) as excinfo:
        export_cookies(profile)
    assert "locked by a running browser" in str(excinfo.value)
    assert "--remote-debugging-port=9222" in str(excinfo.value)


def test_cdp_ports_env(monkeypatch):
    monkeypatch.delenv("CAMOFOX_SEARCH_CDP_PORTS", raising=False)
    assert cookies_module.cdp_ports() == [9222]
    monkeypatch.setenv("CAMOFOX_SEARCH_CDP_PORTS", "9222, 9333,bogus")
    assert cookies_module.cdp_ports() == [9222, 9333]


def test_export_via_cdp_maps_and_filters(monkeypatch):
    now = int(time.time())
    captured: dict = {}

    def _fake_command(ws_url, method, params=None, **kwargs):
        captured["method"] = method
        return {"cookies": [
            {"name": "keep", "value": "v", "domain": ".example.com", "path": "/",
             "expires": now + 100, "httpOnly": True, "secure": True, "sameSite": "Lax"},
            {"name": "session", "value": "s", "domain": ".example.com", "path": "/",
             "expires": -1, "httpOnly": False, "secure": False, "sameSite": "None"},
            {"name": "expired", "value": "e", "domain": ".example.com", "path": "/",
             "expires": now - 10, "httpOnly": False, "secure": True},
            {"name": "other", "value": "o", "domain": ".other.org", "path": "/",
             "expires": -1, "httpOnly": False, "secure": True},
        ]}

    monkeypatch.setattr(cookies_module, "cdp_command", _fake_command)
    result = cookies_module.export_via_cdp(9222, ws_url="ws://127.0.0.1:9222/devtools/browser/x")

    assert captured["method"] == "Network.getAllCookies"
    by_name = {c["name"]: c for c in result.cookies}
    assert set(by_name) == {"keep", "session", "other"}
    assert by_name["keep"]["sameSite"] == "Lax"
    assert "sameSite" not in by_name["session"]  # None + insecure → dropped
    assert result.stats["rows"] == 4 and result.stats["exported"] == 3
    assert result.profile.browser == "cdp"

    filtered = cookies_module.export_via_cdp(
        9222, ws_url="ws://x", domains=["other.org"])
    assert [c["name"] for c in filtered.cookies] == ["other"]


def test_export_via_cdp_without_endpoint_raises(monkeypatch):
    monkeypatch.setattr(cookies_module, "cdp_page_ws_url", lambda port, **kw: None)
    with pytest.raises(cookies_module.CdpError):
        cookies_module.export_via_cdp(9222)


# ---- import --------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []
        self.batch_payloads: list[list[dict]] = []

    def set_cookies(self, user_id, cookies, tab_id=None):
        self.calls.append(("set_cookies", user_id, len(cookies), tab_id))
        self.batch_payloads.append(list(cookies))
        return {"ok": True, "userId": user_id, "count": len(cookies)}


def test_import_cookies_batches_at_500_and_reports_counts():
    client = FakeClient()
    cookies = [{"name": f"c{i}", "value": "v", "domain": "x.com",
                "path": "/", "expires": -1, "httpOnly": False, "secure": False}
               for i in range(1201)]

    result = import_cookies(client, "camofox_search", cookies, tab_id="tab_1")

    assert [call[2] for call in client.calls] == [500, 500, 201]
    assert all(call[1] == "camofox_search" and call[3] == "tab_1"
               for call in client.calls)
    assert result.batches == 3
    assert result.sent == 1201
    assert result.accepted == 1201
    assert result.to_dict() == {"sent": 1201, "accepted": 1201, "batches": 3,
                                "present_after": None}


def test_import_cookies_splits_batches_by_payload_bytes():
    """Long values must split below the count cap: the daemon 413s >100 KB."""
    client = FakeClient()
    cookies = [{"name": f"c{i}", "value": "x" * 400, "domain": "example.com",
                "path": "/", "expires": -1, "httpOnly": False, "secure": True}
               for i in range(300)]

    result = import_cookies(client, "u", cookies)

    counts = [call[2] for call in client.calls]
    assert result.sent == 300
    assert len(counts) > 1  # byte cap forces more than one batch
    assert max(counts) <= cookies_module.MAX_COOKIES_PER_REQUEST
    for batch in client.batch_payloads:
        wire = len(json.dumps(batch, ensure_ascii=True).encode("utf-8"))
        assert wire <= cookies_module.MAX_BATCH_BYTES


def test_import_cookies_tolerates_response_without_count():
    class Silent(FakeClient):
        def set_cookies(self, user_id, cookies, tab_id=None):
            self.calls.append(("set_cookies", user_id, len(cookies), tab_id))
            return {}

    client = Silent()
    result = import_cookies(client, "u", [{"name": "a", "value": "b",
                                           "domain": "x", "path": "/"}])
    assert result.sent == result.accepted == 1
