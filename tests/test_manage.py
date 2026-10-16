"""Offline tests for the camofox-search-manage tool handler (manage.py)."""

from __future__ import annotations

import json

from plugin.camofox_search import rules
from plugin.camofox_search.manage import MANAGE_SCHEMA, handle_manage


def _call(**args):
    """Invoke the handler like the tool registry does and parse the envelope."""
    raw = handle_manage(args)
    assert isinstance(raw, str)
    return json.loads(raw)


def test_schema_shape():
    assert MANAGE_SCHEMA["name"] == "camofox-search-manage"
    params = MANAGE_SCHEMA["parameters"]
    assert params["required"] == ["action", "rule_type"]
    assert set(params["properties"]["action"]["enum"]) == {
        "list", "get", "create", "update", "delete"}
    assert set(params["properties"]["rule_type"]["enum"]) == {"search", "content"}


def test_list_returns_seeded_search_rules():
    payload = _call(action="list", rule_type="search")
    assert payload["success"] is True
    assert payload["count"] == len(payload["rules"]) == 3
    domains = {rule["domain"] for rule in payload["rules"]}
    assert {"bing.com", "google.com", "duckduckgo.com"} <= domains
    assert all(rule["kind"] == "search" for rule in payload["rules"])
    assert all("url_template" in rule for rule in payload["rules"])


def test_search_rule_create_get_update_delete_flow():
    created = _call(action="create", rule_type="search", domain="sogou.com",
                    url_template="https://www.sogou.com/web?query={query}",
                    extract_js="JSON.stringify({ok:true,results:[]})",
                    notes="cn engine")
    assert created["success"] is True
    assert created["rule"]["domain"] == "sogou.com"
    assert created["rule"]["notes"] == "cn engine"

    fetched = _call(action="get", rule_type="search", domain="https://www.sogou.com/x")
    assert fetched["success"] is True
    assert fetched["rule"]["domain"] == "sogou.com"

    updated = _call(action="update", rule_type="search", domain="sogou.com",
                    extract_js="JSON.stringify({ok:true,results:[1]})")
    assert updated["success"] is True
    assert "results:[1]" in updated["rule"]["extract_js"]
    assert updated["rule"]["url_template"] == created["rule"]["url_template"]

    deleted = _call(action="delete", rule_type="search", domain="sogou.com")
    assert deleted["success"] is True and deleted["deleted"] is True
    assert deleted["rule"]["domain"] == "sogou.com"
    assert rules.get_search_rule("sogou.com") is None


def test_content_rule_create_get_update_delete_flow():
    created = _call(action="create", rule_type="content", domain="zhihu.com",
                    extract_js="JSON.stringify({ok:true,title:document.title,url:location.href,text:'x'})")
    assert created["success"] is True
    assert created["rule"]["kind"] == "content"
    assert "url_template" not in created["rule"]

    listed = _call(action="list", rule_type="content")
    assert listed["count"] == 1 and listed["rules"][0]["domain"] == "zhihu.com"

    updated = _call(action="update", rule_type="content", domain="zhihu.com",
                    notes="reader layout")
    assert updated["success"] is True and updated["rule"]["notes"] == "reader layout"

    deleted = _call(action="delete", rule_type="content", domain="zhihu.com")
    assert deleted["success"] is True
    assert _call(action="list", rule_type="content")["count"] == 0


def test_validation_errors_are_reported_not_raised():
    cases = [
        ({"action": "explode", "rule_type": "search"}, "invalid action"),
        ({"action": "list", "rule_type": "pages"}, "invalid rule_type"),
        ({"action": "get", "rule_type": "search"}, "domain is required"),
        ({"action": "create", "rule_type": "search", "domain": "x.com",
          "extract_js": "1"}, "url_template is required"),
        ({"action": "create", "rule_type": "search", "domain": "x.com",
          "url_template": "https://x.com/?q={query}"}, "extract_js is required"),
        ({"action": "create", "rule_type": "content", "domain": "x.com",
          "url_template": "https://x.com/?q={query}", "extract_js": "1"},
         "url_template applies to search rules only"),
        ({"action": "create", "rule_type": "search", "domain": "not a domain",
          "url_template": "https://x.com/?q={query}", "extract_js": "1"},
         "invalid domain"),
        ({"action": "create", "rule_type": "search", "domain": "ok.com",
          "url_template": "https://x.com/?n={limit}", "extract_js": "1"},
         "must contain the {query} placeholder"),
        ({"action": "get", "rule_type": "search", "domain": "nope.com"},
         "no search rule"),
        ({"action": "update", "rule_type": "content", "domain": "nope.com",
          "extract_js": "1"}, "no content rule"),
        ({"action": "update", "rule_type": "search", "domain": "bing.com"},
         "nothing to update"),
        ({"action": "delete", "rule_type": "content", "domain": "nope.com"},
         "to delete"),
    ]
    for args, needle in cases:
        payload = _call(**args)
        assert payload["success"] is False, args
        assert needle in payload["error"], (args, payload)


def test_create_existing_domain_uses_update():
    payload = _call(action="create", rule_type="search", domain="bing.com",
                    url_template="https://x.com/?q={query}", extract_js="1")
    assert payload["success"] is False
    assert "already exists" in payload["error"]


def test_non_string_arguments_rejected():
    payload = _call(action="get", rule_type="search", domain=123)
    assert payload["success"] is False
    assert "must be a string" in payload["error"]


def test_handler_tolerates_missing_or_bad_args():
    for args in (None, [], {"action": None, "rule_type": None}):
        raw = handle_manage(args)
        assert isinstance(raw, str)
        payload = json.loads(raw)
        assert payload["success"] is False
