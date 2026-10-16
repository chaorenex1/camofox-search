"""Offline tests for the plugin entry point: register(ctx) wiring."""

from __future__ import annotations

from pathlib import Path

from plugin.camofox_search import register
from plugin.camofox_search.advance import TOOL_NAME as ADVANCE_TOOL_NAME
from plugin.camofox_search.import_cookies import (
    TOOL_NAME as IMPORT_COOKIES_TOOL_NAME,
)
from plugin.camofox_search.manage import TOOL_NAME


class RecordingCtx:
    """Captures every registration the plugin performs."""

    def __init__(self):
        self.providers: list = []
        self.tools: list[dict] = []
        self.skills: list[tuple] = []

    def register_web_search_provider(self, provider):
        self.providers.append(provider)

    def register_tool(self, **kwargs):
        self.tools.append(kwargs)

    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append((name, Path(path), description, dict(frontmatter or {})))


class MinimalCtx:
    """An older Hermes surface: provider registration only."""

    def __init__(self):
        self.providers: list = []

    def register_web_search_provider(self, provider):
        self.providers.append(provider)


def test_register_wires_provider_tools_and_skills():
    ctx = RecordingCtx()
    register(ctx)

    assert [provider.name for provider in ctx.providers] == ["camofox-search"]

    assert [tool["name"] for tool in ctx.tools] == [
        TOOL_NAME, ADVANCE_TOOL_NAME, IMPORT_COOKIES_TOOL_NAME]
    assert all(tool["toolset"] == "camofox-search" for tool in ctx.tools)
    assert [tool["handler"].__name__ for tool in ctx.tools] == \
        ["handle_manage", "handle_advance", "handle_import_cookies"]

    assert [skill[0] for skill in ctx.skills] == \
        ["camofox-search-advance", "camofox-search-rule"]


def test_bundled_skills_ship_name_and_description():
    ctx = RecordingCtx()
    register(ctx)

    for name, path, description, frontmatter in ctx.skills:
        assert path.is_file() and path.name == "SKILL.md", name
        # The <available_skills> row is built from register-time metadata, so
        # the description must be handed over here (not only stored in file).
        assert description.strip(), f"skill description must be registered: {name}"
        assert frontmatter.get("name") == name
        assert frontmatter.get("description") == description


def test_advance_skill_ships_reference_files():
    ctx = RecordingCtx()
    register(ctx)

    advance = next(skill for skill in ctx.skills if skill[0] == "camofox-search-advance")
    references = advance[1].parent / "references"
    assert sorted(p.name for p in references.glob("*.md")) == \
        ["engines-cn.md", "engines-intl.md"]


def test_register_tolerates_capability_less_ctx():
    ctx = MinimalCtx()
    register(ctx)  # must warn, not raise

    assert [provider.name for provider in ctx.providers] == ["camofox-search"]


def test_register_initializes_rule_database_with_builtin_engines():
    from plugin.camofox_search import rules as rule_store
    from plugin.camofox_search.engines import ENGINES

    ctx = RecordingCtx()
    register(ctx)

    listed = {rule.domain for rule in rule_store.list_search_rules()}
    assert listed == {"duckduckgo.com", "google.com", "bing.com"}
    assert rule_store.count_rules()["search_rules"] == len(ENGINES)


def test_register_survives_rule_store_failure(monkeypatch):
    from plugin.camofox_search import rules as rule_store

    def boom():
        raise RuntimeError("rule database unavailable")

    monkeypatch.setattr(rule_store, "ensure_db", boom)
    ctx = RecordingCtx()

    register(ctx)  # must warn, not raise

    assert [provider.name for provider in ctx.providers] == ["camofox-search"]
    assert [tool["name"] for tool in ctx.tools] == [
        TOOL_NAME, ADVANCE_TOOL_NAME, IMPORT_COOKIES_TOOL_NAME]


def test_frontmatter_parser_handles_quotes_and_block_scalars():
    from plugin.camofox_search import _parse_frontmatter

    fields = _parse_frontmatter(
        '---\n'
        'name: demo\n'
        'description: "a: b"\n'
        'notes: >\n'
        '  one\n'
        '  two\n'
        '---\n'
        'body\n')

    assert fields["name"] == "demo"
    assert fields["description"] == "a: b"
    assert fields["notes"] == "one two"


def test_frontmatter_parser_ignores_text_without_fence():
    from plugin.camofox_search import _parse_frontmatter

    assert _parse_frontmatter("# no frontmatter\nkey: value\n") == {}
