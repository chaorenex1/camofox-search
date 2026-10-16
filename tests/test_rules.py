"""Offline tests for the SQLite rule store (rules.py).

The autouse ``_isolated_rule_data_dir`` fixture in conftest.py already points
``CAMOFOX_SEARCH_DATA_DIR`` at a per-test temp directory.
"""

from __future__ import annotations

import sqlite3

import pytest

from plugin.camofox_search import rules
from plugin.camofox_search.engines import ENGINES


# ---- location / schema ----


def test_default_data_dir_layout(monkeypatch, tmp_path):
    monkeypatch.delenv("CAMOFOX_SEARCH_DATA_DIR", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    assert rules.data_dir() == tmp_path / "home" / "camofox-search"
    assert rules.db_path().name == "camofox-search.db"


def test_explicit_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path / "custom"))
    assert rules.data_dir() == tmp_path / "custom"


def test_ensure_db_creates_wal_database(tmp_path, monkeypatch):
    target = tmp_path / "camofox-search"
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(target))

    path = rules.ensure_db()

    assert path == target / "camofox-search.db"
    assert path.is_file()
    conn = sqlite3.connect(str(path))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        conn.close()
    assert mode.lower() == "wal"
    assert {"search_rules", "content_rules"} <= tables


def test_table_columns_match_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    conn = rules.connect()
    try:
        search_cols = {row[1] for row in conn.execute("PRAGMA table_info(search_rules)")}
        content_cols = {row[1] for row in conn.execute("PRAGMA table_info(content_rules)")}
    finally:
        conn.close()
    assert {"domain", "url_template", "extract_js"} <= search_cols
    assert {"domain", "extract_js"} <= content_cols


def test_seeds_builtin_engines(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    listed = {rule.domain for rule in rules.list_search_rules()}
    assert listed == {engine.domain for engine in ENGINES.values()}
    bing = rules.get_search_rule("bing.com")
    assert bing is not None and "@LIMIT@" in bing.extract_js
    assert "{query}" in (bing.url_template or "")


def test_seeding_is_idempotent_and_never_overwrites_edits(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    rules.update_search_rule("bing.com", url_template="https://edited.example/?q={query}",
                             notes="customized")
    before = rules.count_rules()

    monkeypatch.setattr(rules, "_ENSURED_DBS", set())  # simulate a fresh process
    rules.ensure_db()

    after = rules.count_rules()
    assert after == before
    edited = rules.get_search_rule("bing.com")
    assert edited is not None
    assert edited.url_template == "https://edited.example/?q={query}"
    assert edited.notes == "customized"


# ---- validation ----


@pytest.mark.parametrize("value,expected", [
    ("bing.com", "bing.com"),
    ("Bing.COM", "bing.com"),
    ("www.example.com", "example.com"),
    ("https://www.Example.com/path?q=1", "example.com"),
    ("example.com/page", "example.com"),
    ("localhost", "localhost"),
])
def test_normalize_domain(value, expected):
    assert rules.normalize_domain(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "not a domain", "http://", "a b.com"])
def test_normalize_domain_rejects(value):
    with pytest.raises(rules.RuleValidationError):
        rules.normalize_domain(value)


def test_validate_url_template_rules():
    with pytest.raises(rules.RuleValidationError):
        rules.validate_url_template("")
    with pytest.raises(rules.RuleValidationError):
        rules.validate_url_template("ftp://example.com?q={query}")
    with pytest.raises(rules.RuleValidationError):
        rules.validate_url_template("https://example.com/?q=no-placeholder")
    with pytest.raises(rules.RuleValidationError):
        rules.validate_url_template("https://example.com/?q={query}&p={page}")
    ok = rules.validate_url_template("https://example.com/?q={query}&n={limit}")
    assert ok.startswith("https://")


def test_validate_extract_js_rules():
    with pytest.raises(rules.RuleValidationError):
        rules.validate_extract_js("")
    with pytest.raises(rules.RuleValidationError):
        rules.validate_extract_js("x" * (rules.MAX_JS_CHARS + 1))
    with pytest.raises(rules.RuleValidationError):
        rules.validate_extract_js("bad\u0000js")
    assert rules.validate_extract_js("  document.title  ") == "document.title"


# ---- search-rule CRUD ----


def test_search_rule_crud_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    created = rules.create_search_rule(
        "sogou.com", "https://www.sogou.com/web?query={query}&page={limit}",
        "JSON.stringify({ok:true,results:[]})", notes="new engine")
    assert created.id > 0 and created.kind == "search"

    fetched = rules.get_search_rule("www.sogou.com")
    assert fetched is not None and fetched.domain == "sogou.com"

    updated = rules.update_search_rule("sogou.com", notes="tuned",
                                       extract_js="JSON.stringify({ok:true,results:[1]})")
    assert updated.notes == "tuned"
    assert "results:[1]" in updated.extract_js
    assert updated.url_template == created.url_template  # untouched by partial update
    assert updated.created_at == created.created_at

    deleted = rules.delete_search_rule("sogou.com")
    assert deleted.domain == "sogou.com"
    assert rules.get_search_rule("sogou.com") is None


def test_search_rule_create_duplicate_and_update_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    rules.create_search_rule("sogou.com", "https://s.example/?q={query}", "1")
    with pytest.raises(rules.RuleExistsError):
        rules.create_search_rule("sogou.com", "https://s.example/?q={query}", "1")
    with pytest.raises(rules.RuleNotFoundError):
        rules.update_search_rule("missing.com", notes="x")
    with pytest.raises(rules.RuleValidationError):
        rules.update_search_rule("sogou.com")  # nothing to update
    with pytest.raises(rules.RuleNotFoundError):
        rules.delete_search_rule("missing.com")


# ---- content-rule CRUD ----


def test_content_rule_crud_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    created = rules.create_content_rule("example.com", "document.title", notes="demo")
    assert created.kind == "content" and created.url_template is None
    assert "url_template" not in created.to_dict()

    updated = rules.update_content_rule("example.com", extract_js="1+1")
    assert updated.extract_js == "1+1"
    assert updated.notes == "demo"

    assert rules.delete_content_rule("example.com").domain == "example.com"
    assert rules.list_content_rules() == []


def test_content_rule_duplicate_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    rules.create_content_rule("example.com", "document.title")
    with pytest.raises(rules.RuleExistsError):
        rules.create_content_rule("example.com", "document.title")
    with pytest.raises(rules.RuleNotFoundError):
        rules.update_content_rule("nope.com", extract_js="1")
    with pytest.raises(rules.RuleValidationError):
        rules.update_content_rule("example.com")  # nothing to update


# ---- resolution ----


def test_resolve_search_rule_matches_id_domain_and_subdomain(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    assert rules.resolve_search_rule("bing").domain == "bing.com"
    assert rules.resolve_search_rule("bing.com").domain == "bing.com"
    assert rules.resolve_search_rule("cn.bing.com").domain == "bing.com"
    assert rules.resolve_search_rule("Bing").domain == "bing.com"
    assert rules.resolve_search_rule("yandex") is None


def test_resolve_search_rule_finds_custom_engines(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    rules.create_search_rule("yandex.com", "https://yandex.com/search/?text={query}", "1")
    assert rules.resolve_search_rule("yandex").domain == "yandex.com"
    assert rules.resolve_search_rule("yandex.ru") is None


def test_resolve_content_rule_domain_and_subdomain(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    rules.create_content_rule("example.com", "1")
    assert rules.resolve_content_rule("https://example.com/a").domain == "example.com"
    assert rules.resolve_content_rule("https://news.example.com/a").domain == "example.com"
    assert rules.resolve_content_rule("example.com") is not None
    assert rules.resolve_content_rule("https://notexample.com/") is None


def test_resolve_content_rule_longest_domain_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    rules.create_content_rule("example.com", "1")
    rules.create_content_rule("news.example.com", "2")
    assert rules.resolve_content_rule("https://news.example.com/x").domain == "news.example.com"
    assert rules.resolve_content_rule("https://other.example.com/x").domain == "example.com"


def test_count_rules_reports_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(tmp_path))
    counts = rules.count_rules()
    assert counts["search_rules"] == len(ENGINES)
    assert counts["content_rules"] == 0
