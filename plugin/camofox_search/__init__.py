"""camofox-search — a Hermes web-search provider backed by the local Camofox browser.

Serviced capabilities: ``web_search`` and ``web_extract`` (selected via
``web.search_backend`` / ``web.extract_backend`` / ``web.backend``).

At plugin startup the rule database (``HERMES_HOME/camofox-search/
camofox-search.db``) is created and seeded with the built-in search rules
(duckduckgo / google / bing).

Also bundled:

- toolset ``camofox-search``:
  - ``camofox-search-manage`` — CRUD over the plugin's search/content rule
    database (``HERMES_HOME/camofox-search/camofox-search.db``);
  - ``camofox-search-advance`` — advanced search: per-engine SERP URLs with
    operators and time/language/region/vertical/page filters, optionally
    executed through Camofox;
  - ``camofox-search-import-cookies`` — import cookies from the user's local
    browser into the plugin's Camofox profile (manual trigger for the
    scheduled refresh);
- skills (load as ``web-camofox-search:<name>``):
  - ``camofox-search-rule`` — guidance for managing those rules proactively;
  - ``camofox-search-advance`` — engine catalog and advanced-search workflows.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import rules as rule_store
from .advance import ADVANCE_SCHEMA, TOOL_NAME as ADVANCE_TOOL_NAME, handle_advance
from .import_cookies import (
    IMPORT_COOKIES_SCHEMA,
    TOOL_NAME as IMPORT_COOKIES_TOOL_NAME,
    handle_import_cookies,
)
from .manage import MANAGE_SCHEMA, TOOL_NAME, handle_manage
from .provider import CamofoxSearchProvider

logger = logging.getLogger(__name__)

__all__ = ["CamofoxSearchProvider", "register"]

TOOL_TOOLSET = "camofox-search"

_MANAGE_TOOL_DESCRIPTION = (
    "Manage camofox-search rules (search engines and page extraction) in the "
    "plugin's SQLite database.")

_ADVANCE_TOOL_DESCRIPTION = (
    "Advanced camofox-search: build per-engine SERP URLs (operators, time, "
    "language, region, vertical, pagination) and optionally run them through "
    "the local Camofox browser.")

_IMPORT_COOKIES_TOOL_DESCRIPTION = (
    "Import cookies from the local browser into the Camofox profile "
    "camofox-search browses with.")


def register(ctx) -> None:
    """Hermes plugin entry point: provider + tools + bundled skills."""
    _initialize_rule_store()
    ctx.register_web_search_provider(CamofoxSearchProvider())
    _register_tools(ctx)
    _register_skills(ctx)


def _initialize_rule_store() -> None:
    """Create and seed the rule database at plugin startup (best effort).

    ``rules.ensure_db()`` creates the SQLite file with both tables and fills in
    the built-in search rules (duckduckgo / google / bing) via ``INSERT OR
    IGNORE``, so the engine rules always exist on a fresh start while edits are
    never overwritten. A store problem (unwritable HERMES_HOME) logs a warning
    and must never break plugin startup — the provider then degrades to the
    built-in engines.
    """
    try:
        path = rule_store.ensure_db()
    except Exception as exc:  # noqa: BLE001 - startup must not fail the plugin
        logger.warning("camofox-search: rule database initialization failed: %s", exc)
        return
    logger.info("camofox-search: rule database initialized at %s", path)


def _register_tools(ctx) -> None:
    register_tool = getattr(ctx, "register_tool", None)
    if register_tool is None:  # older Hermes without tool registration
        logger.warning("camofox-search: ctx.register_tool unavailable; "
                       "%s/%s/%s tools not registered", TOOL_NAME,
                       ADVANCE_TOOL_NAME, IMPORT_COOKIES_TOOL_NAME)
        return
    for name, schema, handler, emoji, description in (
        (TOOL_NAME, MANAGE_SCHEMA, handle_manage, "🔎", _MANAGE_TOOL_DESCRIPTION),
        (ADVANCE_TOOL_NAME, ADVANCE_SCHEMA, handle_advance, "🧭", _ADVANCE_TOOL_DESCRIPTION),
        (IMPORT_COOKIES_TOOL_NAME, IMPORT_COOKIES_SCHEMA, handle_import_cookies,
         "🍪", _IMPORT_COOKIES_TOOL_DESCRIPTION),
    ):
        try:
            register_tool(name=name, toolset=TOOL_TOOLSET, schema=schema,
                          handler=handler, emoji=emoji, description=description)
        except Exception as exc:  # noqa: BLE001 - never let a tool kill the plugin
            logger.warning("camofox-search: registering %s failed: %s", name, exc)


def _register_skills(ctx) -> None:
    register_skill = getattr(ctx, "register_skill", None)
    if register_skill is None:  # older Hermes without plugin skills
        logger.warning("camofox-search: ctx.register_skill unavailable; "
                       "bundled skills not registered")
        return
    skills_dir = Path(__file__).parent / "skills"
    if not skills_dir.is_dir():
        return
    for child in sorted(skills_dir.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.exists():
            description, frontmatter = _skill_metadata(skill_md)
            try:
                register_skill(child.name, skill_md, description=description,
                               frontmatter=frontmatter)
            except Exception as exc:  # noqa: BLE001
                logger.warning("camofox-search: registering skill %s failed: %s",
                               child.name, exc)


def _skill_metadata(skill_md: Path) -> tuple[str, dict]:
    """``(description, frontmatter)`` from a bundled SKILL.md.

    Hermes builds a plugin skill's ``<available_skills>`` row from what is
    passed to ``register_skill`` — the on-disk frontmatter is not re-read for
    that row — so the description must be handed over at registration time.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("camofox-search: cannot read %s: %s", skill_md, exc)
        return "", {}
    frontmatter = _parse_frontmatter(text)
    if not isinstance(frontmatter, dict):
        return "", {}
    description = str(frontmatter.get("description") or "").strip()
    return description, frontmatter


def _parse_frontmatter(text: str) -> dict:
    """Parse the leading YAML frontmatter block of a SKILL.md.

    Dependency-free subset, deliberately avoiding private runtime imports:
    ``key: value`` pairs (quotes stripped) plus ``key: |`` / ``key: >`` block
    scalars — the shapes bundled skills use.
    """
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if not sep or not key.strip():
            index += 1
            continue
        key = key.strip()
        value = value.strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            block: list[str] = []
            index += 1
            while index < len(lines) and (not lines[index].strip()
                                          or lines[index].startswith((" ", "\t"))):
                block.append(lines[index].strip())
                index += 1
            if value.startswith(">"):
                fields[key] = " ".join(part for part in block if part)
            else:
                while block and not block[-1]:
                    block.pop()
                fields[key] = "\n".join(block)
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key] = value
        index += 1
    return fields
