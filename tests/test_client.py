"""Offline tests for the Camofox REST client (HTTP fully faked)."""

from __future__ import annotations

import pytest
import requests

from plugin.camofox_search import client as client_module
from plugin.camofox_search.client import (
    CamofoxAuthError,
    CamofoxClient,
    CamofoxConnectionError,
    CamofoxError,
    parse_eval_result,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class FakeRequests:
    """Stand-in for the ``requests`` module surface the client uses."""

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        result = self.responder(method, url, kwargs)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def fake_requests(monkeypatch):
    def install(responder):
        fake = FakeRequests(responder)
        monkeypatch.setattr(client_module.requests, "request", fake.request)
        return fake

    return install


def _client(**overrides) -> CamofoxClient:
    kwargs = {"base_url": "http://127.0.0.1:9377/", "api_key": "sekrit", "timeout": 5}
    kwargs.update(overrides)
    return CamofoxClient(**kwargs)


def test_create_tab_sends_auth_and_payload(fake_requests):
    fake = fake_requests(lambda method, url, kw: FakeResponse(200, {"tabId": "t1"}))
    tab_id = _client().create_tab("u1", "https://example.com/")

    assert tab_id == "t1"
    call = fake.calls[0]
    assert call["method"] == "post"
    assert call["url"] == "http://127.0.0.1:9377/tabs"  # trailing slash stripped
    assert call["headers"] == {"Authorization": "Bearer sekrit"}
    assert call["json"] == {
        "userId": "u1", "sessionKey": "camofox_search", "url": "https://example.com/",
    }


def test_create_tab_without_key_omits_auth_header(fake_requests):
    fake = fake_requests(lambda method, url, kw: FakeResponse(200, {"tabId": "t1"}))
    _client(api_key="").create_tab("u1", "https://example.com/")
    assert fake.calls[0]["headers"] == {}


def test_create_tab_missing_tab_id_raises(fake_requests):
    fake_requests(lambda method, url, kw: FakeResponse(200, {"ok": True}))
    with pytest.raises(CamofoxError, match="created no tab"):
        _client().create_tab("u1", "https://example.com/")


def test_forbidden_maps_to_auth_error(fake_requests):
    fake_requests(lambda method, url, kw: FakeResponse(403, None, "Forbidden"))
    with pytest.raises(CamofoxAuthError):
        _client().create_tab("u1", "https://example.com/")


def test_http_error_maps_to_camofox_error(fake_requests):
    fake_requests(lambda method, url, kw: FakeResponse(500, None, "boom"))
    with pytest.raises(CamofoxError, match="HTTP 500"):
        _client().create_tab("u1", "https://example.com/")


def test_connection_error_maps_to_camofox_connection_error(fake_requests):
    fake_requests(lambda method, url, kw: requests.ConnectionError("refused"))
    with pytest.raises(CamofoxConnectionError, match="Cannot reach Camofox"):
        _client().create_tab("u1", "https://example.com/")


def test_evaluate_json_string_result_is_parsed(fake_requests):
    fake_requests(lambda method, url, kw: FakeResponse(
        200, {"ok": True, "result": '{"a": 1}', "resultType": "object"}))
    assert _client().evaluate("t1", "u1", "1+1") == {"a": 1}


def test_evaluate_decoded_result_passes_through(fake_requests):
    fake_requests(lambda method, url, kw: FakeResponse(
        200, {"ok": True, "result": {"a": 1}, "resultType": "object"}))
    assert _client().evaluate("t1", "u1", "x") == {"a": 1}


def test_evaluate_plain_string_result_passes_through(fake_requests):
    fake_requests(lambda method, url, kw: FakeResponse(200, {"ok": True, "result": "complete"}))
    assert _client().evaluate("t1", "u1", "document.readyState") == "complete"


def test_evaluate_url_and_timeout(fake_requests):
    fake = fake_requests(lambda method, url, kw: FakeResponse(200, {"ok": True, "result": 1}))
    _client().evaluate("t1", "u1", "2+2", timeout=10)
    call = fake.calls[0]
    assert call["url"] == "http://127.0.0.1:9377/tabs/t1/evaluate"
    assert call["json"] == {"userId": "u1", "expression": "2+2"}
    assert call["timeout"] == 10


def test_close_session_deletes_and_swallows_failures(fake_requests):
    fake = fake_requests(lambda method, url, kw: FakeResponse(404, None, "gone"))
    client = _client()
    assert client.close_session("u1") is None  # no raise despite HTTP 404
    assert fake.calls[0]["method"] == "delete"
    assert fake.calls[0]["url"] == "http://127.0.0.1:9377/sessions/u1"


def test_close_session_swallows_connection_errors(fake_requests):
    fake_requests(lambda method, url, kw: requests.ConnectionError("refused"))
    assert _client().close_session("u1") is None


def test_new_session_id_is_unique_and_prefixed():
    a, b = CamofoxClient.new_session_id(), CamofoxClient.new_session_id()
    assert a.startswith("camofox_search_") and b.startswith("camofox_search_")
    assert a != b


def test_parse_eval_result_variants():
    assert parse_eval_result('{"ok": true}') == {"ok": True}
    assert parse_eval_result("[1, 2]") == [1, 2]
    assert parse_eval_result("plain text") == "plain text"
    assert parse_eval_result("{not json") == "{not json"
    assert parse_eval_result(42) == 42
    assert parse_eval_result(None) is None
