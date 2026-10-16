"""Offline tests for the camofox-search-import-cookies tool (import_cookies.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugin.camofox_search import import_cookies as tool_module
from plugin.camofox_search.cookies import (
    BrowserProfile,
    CdpError,
    CookieSourceLockedError,
    ExportResult,
)
from plugin.camofox_search.import_cookies import (
    IMPORT_COOKIES_SCHEMA,
    handle_import_cookies,
)


@pytest.fixture(autouse=True)
def _offline_cdp_probe(monkeypatch):
    """Never probe real CDP ports during tests."""
    monkeypatch.setattr(tool_module, "cdp_reachable", lambda port, **kw: False)


def _profile(tmp_path: Path, browser: str = "edge", name: str = "Default") -> BrowserProfile:
    return BrowserProfile(browser=browser, family="chromium", name=name,
                          cookies_path=tmp_path / "Cookies",
                          user_data_dir=tmp_path, last_modified=100.0)


def _cookie(i: int) -> dict:
    return {"name": f"c{i}", "value": f"v{i}", "domain": ".example.com",
            "path": "/", "expires": -1, "httpOnly": False, "secure": True}


class FakeClient:
    """CamofoxClient double: tab lifecycle + cookie endpoints recorded."""

    instances: list["FakeClient"] = []
    tab_seq = 0
    present_count = 0

    def __init__(self, *args, **kwargs):
        self.calls: list[tuple] = []
        FakeClient.instances.append(self)

    def create_tab(self, user_id, url="", session_key="camofox_search"):
        self.calls.append(("create_tab", user_id, url))
        FakeClient.tab_seq += 1
        return f"tab_{FakeClient.tab_seq}"

    def set_cookies(self, user_id, cookies, tab_id=None):
        self.calls.append(("set_cookies", user_id, len(cookies), tab_id))
        return {"ok": True, "userId": user_id, "count": len(cookies)}

    def get_tab_cookies(self, tab_id, user_id=None):
        self.calls.append(("get_tab_cookies", tab_id, user_id))
        return [{"name": f"n{i}", "domain": ".example.com"} for i in range(self.present_count)]

    def close_tab(self, tab_id, user_id):
        self.calls.append(("close_tab", tab_id, user_id))

    def list_tabs(self, user_id=None):
        self.calls.append(("list_tabs", user_id))
        return [{"tabId": "holder", "url": "about:blank"}]


@pytest.fixture()
def fake_client(monkeypatch):
    FakeClient.instances = []
    FakeClient.tab_seq = 0
    FakeClient.present_count = 0
    monkeypatch.setattr(tool_module, "CamofoxClient", FakeClient)
    monkeypatch.setattr(tool_module, "camofox_user_id", lambda: "camofox_search")
    return FakeClient


@pytest.fixture()
def detected(tmp_path, monkeypatch):
    profile = _profile(tmp_path)
    monkeypatch.setattr(tool_module, "detect_browsers", lambda: [profile])
    return profile


def _export(cookies, profile=None):
    return ExportResult(profile=profile, cookies=cookies,
                        stats={"rows": len(cookies), "exported": len(cookies),
                               "expired": 0, "filtered": 0, "undecryptable": 0,
                               "duplicates": 0, "key_error": 0})


def _export_result(profile, cookies):
    return _export(cookies, profile=profile)


def _call(**args):
    raw = handle_import_cookies(args)
    assert isinstance(raw, str)
    return json.loads(raw)


def _calls(client, kind):
    return [call for call in client.calls if call[0] == kind]


# ---- schema --------------------------------------------------------------


def test_schema_shape():
    assert IMPORT_COOKIES_SCHEMA["name"] == "camofox-search-import-cookies"
    params = IMPORT_COOKIES_SCHEMA["parameters"]
    assert params["required"] == ["action"]
    assert params["properties"]["action"]["enum"] == ["detect", "run"]
    browsers = params["properties"]["browser"]["enum"]
    assert browsers[0] == "auto" and "edge" in browsers and "firefox" in browsers


# ---- detect --------------------------------------------------------------


def test_detect_lists_profiles(detected):
    payload = _call(action="detect")
    assert payload["success"] is True
    assert payload["action"] == "detect"
    assert payload["count"] == 1
    row = payload["browsers"][0]
    assert row["browser"] == "edge" and row["profile"] == "Default"
    assert "cookies_path" in row and "hint" in payload
    assert payload["cdp"] == [{"port": 9222, "reachable": False}]


def test_detect_empty_is_reported(monkeypatch):
    monkeypatch.setattr(tool_module, "detect_browsers", lambda: [])
    payload = _call(action="detect")
    assert payload["count"] == 0 and payload["browsers"] == []


# ---- run -----------------------------------------------------------------


def test_run_exports_imports_and_verifies(fake_client, detected, monkeypatch):
    monkeypatch.setattr(tool_module, "export_cookies",
                        lambda profile, domains=None: _export(
                            [_cookie(i) for i in range(3)], profile=profile))
    fake_client.present_count = 3

    payload = _call(action="run")

    assert payload["success"] is True
    assert payload["browser"] == "edge" and payload["profile"] == "Default"
    assert payload["user_id"] == "camofox_search"
    assert payload["export"]["stats"]["exported"] == 3
    assert payload["import"] == {"sent": 3, "accepted": 3, "batches": 1,
                                 "present_after": 3}
    assert payload["verified"] is True

    client = fake_client.instances[0]
    assert _calls(client, "create_tab")[0][1] == "camofox_search"
    assert _calls(client, "create_tab")[0][2] == ""  # blank tab until cookies land
    assert _calls(client, "set_cookies")[0] == ("set_cookies", "camofox_search", 3, "tab_1")
    assert len(_calls(client, "close_tab")) == 1


def test_run_dry_run_stops_after_export(fake_client, detected, monkeypatch):
    monkeypatch.setattr(tool_module, "export_cookies",
                        lambda profile, domains=None: _export([_cookie(0)], profile=profile))
    payload = _call(action="run", dry_run=True)
    assert payload["dry_run"] is True and "import" not in payload
    assert fake_client.instances == []


def test_run_with_nothing_to_export(fake_client, detected, monkeypatch):
    monkeypatch.setattr(tool_module, "export_cookies",
                        lambda profile, domains=None: _export([], profile=profile))
    payload = _call(action="run")
    assert payload["imported"] is False and "note" in payload
    assert fake_client.instances == []


def test_run_verified_false_when_round_trip_lower(fake_client, detected, monkeypatch):
    monkeypatch.setattr(tool_module, "export_cookies",
                        lambda profile, domains=None: _export(
                            [_cookie(i) for i in range(4)], profile=profile))
    fake_client.present_count = 1
    payload = _call(action="run")
    assert payload["verified"] is False and "note" in payload


def test_run_honours_filters_and_target(fake_client, detected, monkeypatch):
    seen: dict = {}

    def _export(profile, domains=None):
        seen["domains"] = domains
        seen["profile"] = profile
        return _export_result(profile, [_cookie(0)])

    monkeypatch.setattr(tool_module, "export_cookies", _export)

    payload = _call(action="run", browser="edge", profile="Default",
                    domains=["example.com"], user_id="other_profile")

    assert payload["user_id"] == "other_profile"
    assert seen["domains"] == ["example.com"]
    assert seen["profile"].name == "Default"
    assert _calls(fake_client.instances[0], "create_tab")[0][1] == "other_profile"


def test_run_falls_back_to_cdp_when_file_is_locked(fake_client, detected, monkeypatch):
    tmp_marker = Path(".")

    def _locked(profile, domains=None):
        raise CookieSourceLockedError("cookie database locked by a running browser")

    def _cdp(port, domains=None):
        assert port == 9222
        return _export_result(
            BrowserProfile("cdp", "cdp", "127.0.0.1:9222", tmp_marker, tmp_marker, 1.0),
            [_cookie(0)])

    monkeypatch.setattr(tool_module, "export_cookies", _locked)
    monkeypatch.setattr(tool_module, "cdp_ports", lambda: [9222])
    monkeypatch.setattr(tool_module, "export_via_cdp", _cdp)
    fake_client.present_count = 1

    payload = _call(action="run")

    assert payload["source"] == "cdp"
    assert payload["browser"] == "cdp" and payload["profile"] == "127.0.0.1:9222"
    assert payload["verified"] is True


def test_run_locked_without_cdp_is_actionable(fake_client, detected, monkeypatch):
    def _locked(profile, domains=None):
        raise CookieSourceLockedError(
            "cookie database locked by a running browser; close it")

    def _cdp(port, domains=None):
        raise CdpError("no CDP endpoint on 127.0.0.1:9222")

    monkeypatch.setattr(tool_module, "export_cookies", _locked)
    monkeypatch.setattr(tool_module, "cdp_ports", lambda: [9222])
    monkeypatch.setattr(tool_module, "export_via_cdp", _cdp)

    payload = _call(action="run")

    assert payload["success"] is False
    assert "locked by a running browser" in payload["error"]
    assert "CDP fallback unavailable" in payload["error"]
    assert "127.0.0.1:9222" in payload["error"]
    assert fake_client.instances == []


# ---- errors --------------------------------------------------------------


def test_argument_and_lookup_errors(fake_client, tmp_path, monkeypatch):
    monkeypatch.setattr(tool_module, "detect_browsers", lambda: [_profile(tmp_path)])
    cases = [
        ({}, "invalid action"),
        ({"action": "cook"}, "invalid action"),
        ({"action": "detect", "browser": 5}, "browser must be a string"),
        ({"action": "run", "browser": "opera"}, "unknown browser"),
        ({"action": "run", "profile": "Nope"}, "no browser profile matched"),
        ({"action": "run", "domains": 7}, "domains must be a string or a list"),
        ({"action": "run", "domains": [1]}, "domains entries must be strings"),
    ]
    for args, needle in cases:
        payload = _call(**args)
        assert payload["success"] is False, args
        assert needle in payload["error"], (args, payload)


def test_handler_tolerates_missing_or_bad_args(fake_client):
    for args in (None, [], {"action": None}):
        raw = handle_import_cookies(args)
        assert isinstance(raw, str)
        assert json.loads(raw)["success"] is False
