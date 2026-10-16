"""Offline tests for CamofoxSearchProvider (client double, no network)."""

from __future__ import annotations

import pytest

from plugin.camofox_search import provider as provider_module
from plugin.camofox_search import rules as rule_store
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
    existing_tabs: list = [{"tabId": "holder", "url": "about:blank"}]

    def __init__(self, *args, **kwargs):
        self.calls: list[tuple] = []
        FakeClient.instances.append(self)

    @staticmethod
    def new_session_id() -> str:
        FakeClient.session_seq += 1
        return f"cfs_{FakeClient.session_seq}"

    _next_create_fails = False

    def create_tab(self, user_id, url="", session_key="camofox_search"):
        self.calls.append(("create_tab", user_id, url))
        if FakeClient._next_create_fails:
            FakeClient._next_create_fails = False
            raise CamofoxError("browserContext.newPage: window is null")
        if self.create_error is not None:
            raise self.create_error
        if url in self.create_error_urls:
            raise CamofoxError(f"create failed for {url}")
        return f"tab_{len(FakeClient.instances)}"

    def list_tabs(self, user_id=None):
        self.calls.append(("list_tabs", user_id))
        return list(FakeClient.existing_tabs)

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

    def close_tab(self, tab_id, user_id):
        self.calls.append(("close_tab", tab_id, user_id))

    def close_session(self, user_id):  # pragma: no cover - must not be called
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
    FakeClient.existing_tabs = [{"tabId": "holder", "url": "about:blank"}]
    FakeClient._next_create_fails = False
    monkeypatch.setattr(provider_module, "CamofoxClient", FakeClient)
    monkeypatch.setattr(provider_module, "default_engine", lambda: "bing")
    monkeypatch.setattr(provider_module, "camofox_user_id", lambda: "camofox_search")
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
    # one stable profile: the session/context is NOT torn down, only the tab
    assert create[1] == "camofox_search"
    assert len(_calls(client, "close_tab")) == 1
    assert _calls(client, "close_tab")[0][2] == create[1]
    assert _calls(client, "close_session") == []


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


def test_search_creates_holder_tab_when_profile_has_no_tabs(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": "A", "url": "https://a.example/", "description": "d"}]}
    fake_client.existing_tabs = []

    result = provider.search("q", limit=5)

    assert result["success"] is True
    creates = _calls(fake_client.instances[0], "create_tab")
    # blank holder first (keeps the browser window alive), then the work tab
    assert [(call[1], call[2]) for call in creates] == [
        ("camofox_search", ""),
        ("camofox_search", "https://www.bing.com/search?q=q&count=5")]
    assert len(_calls(fake_client.instances[0], "close_tab")) == 1


def test_search_heals_a_broken_context_once(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": "A", "url": "https://a.example/", "description": "d"}]}
    fake_client._next_create_fails = True  # first work-tab create hits the dead window

    result = provider.search("q", limit=5)

    assert result["success"] is True
    client = fake_client.instances[0]
    assert _calls(client, "close_session")  # broken context dropped
    creates = _calls(client, "create_tab")
    # work tab (failed) → work tab (retried after the context reset)
    assert len(creates) == 2
    assert creates[-1][2].startswith("https://www.bing.com/search")


def test_search_connection_error_returns_envelope_and_cleans_up(fake_client, provider):
    fake_client.create_error = CamofoxConnectionError("Cannot reach Camofox at http://x")

    result = provider.search("q", limit=5)

    assert result["success"] is False
    assert "Cannot reach Camofox" in result["error"]
    assert _calls(fake_client.instances[0], "close_tab") == []  # no tab existed


def test_search_evaluate_error_returns_envelope(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "results": []}
    fake_client.evaluate_error = CamofoxError("HTTP 409 busy")

    result = provider.search("q", limit=5)

    assert result["success"] is False
    assert "HTTP 409" in result["error"]
    assert len(_calls(fake_client.instances[0], "close_tab")) == 1


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
    # per-URL tabs; the failed create left nothing to close
    assert len(_calls(fake_client.instances[0], "close_tab")) == 1


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
    assert len(_calls(fake_client.instances[0], "close_tab")) == 1


# ---- rule-store integration ----


def _evaluate_expressions(client):
    return [call[1] for call in client.calls
            if call[0] == "evaluate" and "readyState" not in call[1]]


def test_search_uses_database_rule(fake_client, provider):
    rule_store.update_search_rule(
        "bing.com",
        url_template="https://serp.example/search?q={query}&n={limit}",
        extract_js="JSON.stringify({ok:true,results:[{title:'RULED',url:'https://r.example/'}]})",
        notes="custom bing")
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": "RULED", "url": "https://r.example/", "description": "d"}]}

    result = provider.search("rule query", limit=5)

    assert result["success"] is True
    assert [row["title"] for row in result["data"]["web"]] == ["RULED"]
    client = fake_client.instances[0]
    assert _calls(client, "create_tab")[0][2] == \
        "https://serp.example/search?q=rule+query&n=5"
    expressions = _evaluate_expressions(client)
    assert expressions and "RULED" in expressions[0]
    assert "@LIMIT@" not in expressions[0]  # placeholder substituted before evaluate


def test_search_rule_adds_engine_without_builtin_support(fake_client, monkeypatch, provider):
    monkeypatch.setattr(provider_module, "default_engine", lambda: "yandex")
    rule_store.create_search_rule(
        "yandex.com", "https://yandex.example/?text={query}", "JSON.stringify({ok:true,results:[]})")
    fake_client.evaluate_result = {"ok": True, "results": []}

    result = provider.search("q", limit=5)

    assert result["success"] is True
    url = _calls(fake_client.instances[0], "create_tab")[0][2]
    assert url == "https://yandex.example/?text=q"


def test_search_falls_back_to_builtin_when_rules_unavailable(fake_client, monkeypatch, provider):
    def boom(selector):
        raise RuntimeError("rule database unavailable")

    monkeypatch.setattr(rule_store, "resolve_search_rule", boom)
    fake_client.evaluate_result = {"ok": True, "results": [
        {"title": "B", "url": "https://b.example/", "description": ""}]}

    result = provider.search("q", limit=5)

    assert result["success"] is True
    client = fake_client.instances[0]
    assert "bing.com/search?q=q" in _calls(client, "create_tab")[0][2]
    assert any("b_algo" in expr for expr in _evaluate_expressions(client))


def test_search_unknown_engine_error_mentions_manage_tool(monkeypatch, fake_client, provider):
    monkeypatch.setattr(provider_module, "default_engine", lambda: "yandex")

    result = provider.search("q", limit=5)

    assert result["success"] is False
    assert "Unknown search engine" in result["error"]
    assert "camofox-search-manage" in result["error"]


def test_extract_uses_content_rule(fake_client, provider):
    rule_js = "/* RULE_MARK */ JSON.stringify({ok:true,title:'CR',url:location.href,text:'rule body'})"
    rule_store.create_content_rule("good.example", rule_js)
    fake_client.evaluate_result = {"ok": True, "title": "CR",
                                   "url": "https://good.example/", "text": "rule body"}

    result = provider.extract(["https://good.example/page"])

    assert result["success"] is True
    entry = result["data"][0]
    assert entry["title"] == "CR"
    assert entry["content"] == "rule body"
    assert entry["raw_content"] == "rule body"
    assert entry["metadata"]["extract_rule"] == "good.example"
    assert _evaluate_expressions(fake_client.instances[0]) == [rule_js]


def test_extract_rule_string_result_is_content(fake_client, provider):
    rule_store.create_content_rule("str.example", "'plain string body'")
    fake_client.evaluate_result = "plain string body"

    result = provider.extract(["https://str.example/"])

    entry = result["data"][0]
    assert entry["content"] == "plain string body"
    assert entry["metadata"]["extract_rule"] == "str.example"


def test_extract_content_rule_falls_back_to_default(fake_client, provider):
    rule_js = "/* RULE_MARK */ 'nothing useful'"
    rule_store.create_content_rule("good.example", rule_js)
    fake_client.evaluate_queue = [
        {"ok": False},
        {"ok": True, "title": "D", "url": "https://good.example/final", "text": "default body"},
    ]

    result = provider.extract(["https://good.example/"])

    entry = result["data"][0]
    assert entry["content"] == "default body"
    assert entry["metadata"]["extract_rule"] == "default"
    assert entry["metadata"]["extract_rule_attempted"] == "good.example"
    expressions = _evaluate_expressions(fake_client.instances[0])
    assert len(expressions) == 2
    assert expressions[0] == rule_js


def test_extract_default_metadata_without_rule(fake_client, provider):
    fake_client.evaluate_result = {"ok": True, "title": "T", "url": "u", "text": "x"}

    result = provider.extract(["https://no-rule.example/"])

    metadata = result["data"][0]["metadata"]
    assert metadata["extract_rule"] == "default"
    assert "extract_rule_attempted" not in metadata


def test_extract_falls_back_when_rule_lookup_fails(fake_client, monkeypatch, provider):
    def boom(url):
        raise RuntimeError("rule database unavailable")

    monkeypatch.setattr(rule_store, "resolve_content_rule", boom)
    fake_client.evaluate_result = {"ok": True, "title": "T", "url": "u", "text": "x"}

    result = provider.extract(["https://a.example/"])

    assert result["success"] is True
    assert result["data"][0]["content"] == "x"
