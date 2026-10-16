"""Offline tests for the camofox-search-advance tool handler (advance.py)."""

from __future__ import annotations

import json

import pytest

from plugin.camofox_search import rules
from plugin.camofox_search import advance as advance_module
from plugin.camofox_search.advance import ADVANCE_SCHEMA, handle_advance
from plugin.camofox_search.advanced import GLOBAL_VERTICALS


class FakeClient:
    """CamofoxClient double: tab lifecycle recorded, evaluate canned."""

    instances: list["FakeClient"] = []
    session_seq = 0
    timeout = 45.0
    ready_state = "complete"
    evaluate_result = None

    def __init__(self, *args, **kwargs):
        self.calls: list[tuple] = []
        FakeClient.instances.append(self)

    @staticmethod
    def new_session_id() -> str:
        FakeClient.session_seq += 1
        return f"cfs_{FakeClient.session_seq}"

    def create_tab(self, user_id, url, session_key="camofox_search"):
        self.calls.append(("create_tab", user_id, url))
        return f"tab_{len(FakeClient.instances)}"

    def evaluate(self, tab_id, user_id, expression, timeout=None):
        self.calls.append(("evaluate", expression))
        if "readyState" in expression:
            return self.ready_state
        return self.evaluate_result

    def close_session(self, user_id):
        self.calls.append(("close_session", user_id))


@pytest.fixture()
def fake_client(monkeypatch):
    FakeClient.instances = []
    FakeClient.session_seq = 0
    FakeClient.ready_state = "complete"
    FakeClient.evaluate_result = None
    monkeypatch.setattr(advance_module, "CamofoxClient", FakeClient)
    monkeypatch.setattr(advance_module, "default_engine", lambda: "bing")
    return FakeClient


def _call(**args):
    raw = handle_advance(args)
    assert isinstance(raw, str)
    return json.loads(raw)


def _calls(client, kind):
    return [call for call in client.calls if call[0] == kind]


# ---- schema --------------------------------------------------------------


def test_schema_shape():
    assert ADVANCE_SCHEMA["name"] == "camofox-search-advance"
    params = ADVANCE_SCHEMA["parameters"]
    assert params["required"] == ["query"]
    assert params["properties"]["mode"]["enum"] == ["run", "url"]
    assert params["properties"]["vertical"]["enum"] == list(GLOBAL_VERTICALS)
    assert params["properties"]["limit"]["maximum"] == 20


# ---- url mode ------------------------------------------------------------


def test_url_mode_builds_without_executing(fake_client):
    payload = _call(query="hermes agent", engine="google", mode="url",
                    time="week", limit=8)

    assert payload["success"] is True
    assert payload["mode"] == "url"
    assert payload["engine"] == "google"
    assert payload["engine_name"] == "Google"
    assert payload["url"].startswith("https://www.google.com/search?q=hermes+agent")
    assert "tbs=qdr%3Aw" in payload["url"] and "num=8" in payload["url"]
    assert payload["applied"] == {"time": "week", "limit": 8}
    assert payload["unsupported"] == []
    # seeded rule for google.com resolves the extractor
    assert payload["extractor"] == "rule:google.com"
    assert "executed" not in payload and "results" not in payload
    assert payload["hint"]
    assert fake_client.instances == []


def test_url_mode_reports_unsupported_options(fake_client):
    payload = _call(query="x", engine="baidu", mode="url", time="week",
                    language="en")
    assert payload["unsupported"] == ["time", "language"]
    assert payload["url"] == "https://www.baidu.com/s?wd=x"


# ---- run mode ------------------------------------------------------------


def test_run_mode_bing_builds_advanced_url_and_returns_rows(fake_client):
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": "A", "url": "https://a.example/", "description": "da"},
        {"title": "dupe", "url": "https://a.example/", "description": "dup"},
        {"title": "B", "url": "https://b.example/", "description": "db"},
    ]}

    payload = _call(query="hermes agent", engine="bing-cn", time="week",
                    page=2, limit=5)

    assert payload["success"] is True
    assert payload["executed"] is True
    assert payload["count"] == 2
    assert [row["url"] for row in payload["results"]] == [
        "https://a.example/", "https://b.example/"]
    assert [row["position"] for row in payload["results"]] == [1, 2]

    client = fake_client.instances[0]
    create = _calls(client, "create_tab")[0]
    assert create[2] == ("https://cn.bing.com/search?q=hermes+agent&ensearch=0"
                         "&filters=ex1%3A%22ez2%22&first=11&count=5")
    # rule-first resolution: cn.bing.com matches the seeded bing.com rule
    assert payload["extractor"] == "rule:bing.com"
    assert len(_calls(client, "close_session")) == 1


def test_run_mode_uses_builtin_extractor_when_no_rule(fake_client):
    rules.delete_search_rule("google.com")
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": "G", "url": "https://g.example/", "description": "d"}]}

    payload = _call(query="q", engine="google", limit=7)

    assert payload["extractor"] == "engine:google"
    expressions = [call[1] for call in fake_client.instances[0].calls
                   if call[0] == "evaluate" and "readyState" not in call[1]]
    assert expressions and "div.g" in expressions[0]
    assert "slice(0,7)" in expressions[0]


def test_run_mode_rule_expression_wins_over_builtin(fake_client):
    marker = ("JSON.stringify({ok:true,results:[{title:'R',"
              "url:'https://r.example/',description:'d'}]})")
    rules.update_search_rule("bing.com", extract_js=marker)
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": "R", "url": "https://r.example/", "description": "d"}]}

    payload = _call(query="q", engine="bing")

    assert payload["extractor"] == "rule:bing.com"
    expressions = [call[1] for call in fake_client.instances[0].calls
                   if call[0] == "evaluate" and "readyState" not in call[1]]
    assert marker in expressions


def test_run_mode_default_limit_is_ten(fake_client):
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": "A", "url": "https://a.example/", "description": "d"}]}
    payload = _call(query="q", engine="bing")
    assert payload["limit"] == 10
    expressions = [call[1] for call in fake_client.instances[0].calls
                   if call[0] == "evaluate" and "readyState" not in call[1]]
    assert "slice(0,10)" in expressions[0]


def test_run_mode_without_extractor_returns_url_and_reason(fake_client):
    payload = _call(query="机器之心", engine="sogou-wechat", time="week")

    assert payload["success"] is True
    assert payload["executed"] is False
    assert payload["count"] == 0 and payload["results"] == []
    assert payload["extractor"] is None
    assert "sogou-wechat" in payload["engine"]
    assert payload["url"].startswith("https://wx.sogou.com/weixin?type=2&query=")
    assert "camofox-search-manage" in payload["reason"]
    assert payload["unsupported"] == ["time"]
    assert fake_client.instances == []  # nothing was executed


def test_run_mode_zero_rows_carries_verification_note(fake_client):
    fake_client.evaluate_result = {"ok": True, "results": []}
    payload = _call(query="q", engine="bing")
    assert payload["executed"] is True and payload["count"] == 0
    assert "note" in payload and "0 results extracted" in payload["note"]


def test_run_mode_engine_notes_included(fake_client):
    payload = _call(query="integrate x^2 dx", engine="wolframalpha", mode="url")
    assert payload["engine_notes"].startswith("Computation/knowledge engine")


# ---- defaults and errors -------------------------------------------------


def test_default_engine_used_when_omitted(fake_client):
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": "A", "url": "https://a.example/", "description": "d"}]}
    payload = _call(query="q")
    assert payload["engine"] == "bing"


def test_configured_engine_outside_catalog_is_an_error(fake_client, monkeypatch):
    monkeypatch.setattr(advance_module, "default_engine", lambda: "yandex")
    payload = _call(query="q")
    assert payload["success"] is False
    assert "not in the advanced catalog" in payload["error"]


def test_unknown_engine_is_an_error(fake_client):
    payload = _call(query="q", engine="yandex")
    assert payload["success"] is False
    assert "unknown advanced engine" in payload["error"]
    assert "bing-cn" in payload["error"]


def test_argument_validation_errors(fake_client):
    cases = [
        ({}, "query is required"),
        ({"query": "   "}, "query is required"),
        ({"query": "q", "mode": "execute"}, "invalid mode"),
        ({"query": "q", "engine": 5}, "engine must be a string"),
        ({"query": "q", "time": 5}, "time must be a string"),
        ({"query": "q", "page": 0}, "page must be >= 1"),
        ({"query": "q", "page": "two"}, "page must be an integer"),
        ({"query": "q", "limit": "lots"}, "limit must be an integer"),
        ({"query": "q", "time": "yesterday"}, "invalid time value"),
    ]
    for args, needle in cases:
        payload = _call(**args)
        assert payload["success"] is False, args
        assert needle in payload["error"], (args, payload)


def test_handler_tolerates_missing_or_bad_args(fake_client):
    for args in (None, [], {"query": None}):
        raw = handle_advance(args)
        assert isinstance(raw, str)
        assert json.loads(raw)["success"] is False
