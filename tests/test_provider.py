"""Offline tests for CamofoxSearchProvider (client double, no network)."""

from __future__ import annotations

import pytest

from plugin.camofox_search import provider as provider_module
from plugin.camofox_search.client import CamofoxConnectionError, CamofoxError
from plugin.camofox_search.provider import MAX_EXTRACT_URLS, CamofoxSearchProvider


class FakeClient:
    """Configurable CamofoxClient double driven by class-level knobs."""

    instances: list["FakeClient"] = []
    session_seq = 0
    timeout = 45.0
    ready_state = "complete"
    evaluate_result = None
    evaluate_queue: list = []
    create_error = None
    create_error_urls: set[str] = set()
    evaluate_error = None

    def __init__(self, *args, **kwargs):
        self.calls: list[tuple] = []
        FakeClient.instances.append(self)

    @staticmethod
    def new_session_id() -> str:
        FakeClient.session_seq += 1
        return f"cfs_{FakeClient.session_seq}"

    def create_tab(self, user_id, url, session_key="camofox_search"):
        self.calls.append(("create_tab", user_id, url))
        if self.create_error is not None:
            raise self.create_error
        if url in self.create_error_urls:
            raise CamofoxError(f"create failed for {url}")
        return f"tab_{len(FakeClient.instances)}"

    def evaluate(self, tab_id, user_id, expression, timeout=None):
        self.calls.append(("evaluate", expression))
        if "readyState" in expression:
            return self.ready_state
        if self.evaluate_error is not None:
            raise self.evaluate_error
        if self.evaluate_queue:
            if len(self.evaluate_queue) > 1:
                return self.evaluate_queue.pop(0)
            return self.evaluate_queue[0]
        return self.evaluate_result

    def close_session(self, user_id):
        self.calls.append(("close_session", user_id))


@pytest.fixture()
def fake_client(monkeypatch):
    FakeClient.instances = []
    FakeClient.session_seq = 0
    FakeClient.ready_state = "complete"
    FakeClient.evaluate_result = None
    FakeClient.evaluate_queue = []
    FakeClient.create_error = None
    FakeClient.create_error_urls = set()
    FakeClient.evaluate_error = None
    monkeypatch.setattr(provider_module, "CamofoxClient", FakeClient)
    monkeypatch.setattr(provider_module, "default_engine", lambda: "bing")
    return FakeClient


@pytest.fixture()
def provider():
    return CamofoxSearchProvider()


def _calls(client, kind):
    return [call for call in client.calls if call[0] == kind]


# ---- identity / availability ----


def test_identity(provider):
    assert provider.name == "camofox-search"
    assert provider.display_name == "Camofox Search"
    assert provider.supports_search() is True
    assert provider.supports_extract() is True


def test_is_available_follows_camofox_configuration(monkeypatch, provider):
    monkeypatch.setattr(provider_module, "camofox_configured", lambda: True)
    assert provider.is_available() is True
    monkeypatch.setattr(provider_module, "camofox_configured", lambda: False)
    assert provider.is_available() is False


def test_setup_schema_exposes_env_vars(provider):
    schema = provider.get_setup_schema()
    keys = {entry["key"] for entry in schema["env_vars"]}
    assert keys == {"CAMOFOX_URL", "CAMOFOX_API_KEY"}
    assert schema["name"] == "Camofox Search"


# ---- search ----


def test_search_success_envelope(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": "A", "url": "https://a.example/", "description": "da"},
        {"title": "dupe", "url": "https://a.example/", "description": "dup"},
        {"title": "no url", "url": "", "description": "x"},
        {"title": "B", "url": "https://b.example/", "description": "db"},
    ]}

    result = provider.search("hermes agent", limit=5)

    assert result["success"] is True
    rows = result["data"]["web"]
    assert [row["url"] for row in rows] == ["https://a.example/", "https://b.example/"]
    assert [row["position"] for row in rows] == [1, 2]
    client = fake_client.instances[0]
    create = _calls(client, "create_tab")[0]
    assert "bing.com/search?q=hermes+agent" in create[2]
    assert len(_calls(client, "close_session")) == 1
    assert _calls(client, "close_session")[0][1] == create[1]


def test_search_limit_clamped_and_engine_expression(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "results": []}
    provider.search("q", limit=999)
    extractions = [call[1] for call in fake_client.instances[0].calls
                   if call[0] == "evaluate" and "readyState" not in call[1]]
    assert extractions and all("slice(0,20)" in expr for expr in extractions)

    fake_client.instances.clear()
    FakeClient.session_seq = 0
    provider.search("q", limit=0)
    extractions = [call[1] for call in fake_client.instances[0].calls
                   if call[0] == "evaluate" and "readyState" not in call[1]]
    assert extractions and all("slice(0,1)" in expr for expr in extractions)


def test_search_empty_results_retries_then_succeeds(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "results": []}

    result = provider.search("nothing matches this", limit=5)

    assert result["success"] is True
    assert result["data"]["web"] == []
    extractions = [call for call in fake_client.instances[0].calls
                   if call[0] == "evaluate" and "readyState" not in call[1]]
    assert len(extractions) == 3  # bounded retry


def test_search_unknown_engine(monkeypatch, fake_client, provider):
    monkeypatch.setattr(provider_module, "default_engine", lambda: "yandex")
    result = provider.search("q", limit=5)
    assert result["success"] is False
    assert "Unknown search engine" in result["error"]


def test_search_empty_query(provider):
    result = provider.search("   ", limit=5)
    assert result["success"] is False
    assert "Empty search query" in result["error"]


def test_search_connection_error_returns_envelope_and_cleans_up(fake_client, provider):
    fake_client.create_error = CamofoxConnectionError("Cannot reach Camofox at http://x")

    result = provider.search("q", limit=5)

    assert result["success"] is False
    assert "Cannot reach Camofox" in result["error"]
    assert len(_calls(fake_client.instances[0], "close_session")) == 1


def test_search_evaluate_error_returns_envelope(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "results": []}
    fake_client.evaluate_error = CamofoxError("HTTP 409 busy")

    result = provider.search("q", limit=5)

    assert result["success"] is False
    assert "HTTP 409" in result["error"]
    assert len(_calls(fake_client.instances[0], "close_session")) == 1


def test_search_results_trimmed_to_limit(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": f"t{i}", "url": f"https://e{i}.example/", "description": ""}
        for i in range(10)
    ]}
    result = provider.search("q", limit=3)
    assert len(result["data"]["web"]) == 3
    assert [row["position"] for row in result["data"]["web"]] == [1, 2, 3]


# ---- extract ----


def test_extract_success_and_partial_failure(fake_client, provider):
    fake_client.evaluate_result = {
        "ok": True, "title": "Example", "url": "https://final.example/", "text": "Hello body",
    }
    fake_client.create_error_urls = {"https://bad.example/"}

    result = provider.extract(["https://good.example/", "https://bad.example/"])

    assert result["success"] is True
    good, bad = result["data"]
    assert good["url"] == "https://good.example/"
    assert good["title"] == "Example"
    assert good["content"] == "Hello body"
    assert good["raw_content"] == "Hello body"
    assert good["metadata"]["final_url"] == "https://final.example/"
    assert "error" in bad and bad["url"] == "https://bad.example/"
    assert len(fake_client.instances) == 1  # one client handles the whole batch
    assert len(_calls(fake_client.instances[0], "close_session")) == 2  # per-URL sessions


def test_extract_all_failed_returns_error(fake_client, provider):
    fake_client.create_error = CamofoxError("daemon down")

    result = provider.extract(["https://a.example/", "https://b.example/"])

    assert result["success"] is False
    assert "daemon down" in result["error"]


def test_extract_caps_url_count(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "title": "T", "url": "u", "text": "x"}
    urls = [f"https://site{i}.example/" for i in range(MAX_EXTRACT_URLS + 2)]

    result = provider.extract(urls)

    assert result["success"] is True
    assert len(result["data"]) == MAX_EXTRACT_URLS
    assert len(_calls(fake_client.instances[0], "create_tab")) == MAX_EXTRACT_URLS


def test_extract_empty_urls(provider):
    result = provider.extract([])
    assert result["success"] is False
    assert "No URLs" in result["error"]


def test_extract_ignores_extra_kwargs(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "title": "T", "url": "u", "text": "x"}
    result = provider.extract(["https://a.example/"], format="markdown", include_raw=True,
                              max_chars=1000)
    assert result["success"] is True


def test_extract_unreadable_page_reports_error(fake_client, provider):
    fake_client.evaluate_result = {"ok": False}

    result = provider.extract(["https://a.example/"])

    assert result["success"] is False
    assert "could not read page content" in result["error"]
    assert len(_calls(fake_client.instances[0], "close_session")) == 1
