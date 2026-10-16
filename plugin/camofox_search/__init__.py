"""camofox-search — a Hermes web-search provider backed by the local Camofox browser.

Serviced capabilities: ``web_search`` and ``web_extract`` (selected via
``web.search_backend`` / ``web.extract_backend`` / ``web.backend``).

Also bundled:

- toolset ``camofox-search`` — the ``camofox-search-manage`` tool for CRUD over
  the plugin's search/content rule database
  (``HERMES_HOME/camofox-search/camofox-search.db``);
- skill ``camofox-search-rule`` (load as ``web-camofox-search:camofox-search-rule``)
  — guidance for managing those rules proactively.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .manage import MANAGE_SCHEMA, TOOL_NAME, handle_manage
from .provider import CamofoxSearchProvider

logger = logging.getLogger(__name__)

__all__ = ["CamofoxSearchProvider", "register"]

TOOL_TOOLSET = "camofox-search"

_TOOL_DESCRIPTION = (
    "Manage camofox-search rules (search engines and page extraction) in the "
    "plugin's SQLite database.")


def register(ctx) -> None:
    """Hermes plugin entry point: provider + rule tool + bundled skill."""
    ctx.register_web_search_provider(CamofoxSearchProvider())
    _register_manage_tool(ctx)
    _register_skills(ctx)


def _register_manage_tool(ctx) -> None:
    register_tool = getattr(ctx, "register_tool", None)
    if register_tool is None:  # older Hermes without tool registration
        logger.warning("camofox-search: ctx.register_tool unavailable; "
                       "%s tool not registered", TOOL_NAME)
        return
    try:
        register_tool(name=TOOL_NAME, toolset=TOOL_TOOLSET, schema=MANAGE_SCHEMA,
                      handler=handle_manage, emoji="🔎", description=_TOOL_DESCRIPTION)
    except Exception as exc:  # noqa: BLE001 - never let the tool kill the plugin
        logger.warning("camofox-search: registering %s failed: %s", TOOL_NAME, exc)


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
            try:
                register_skill(child.name, skill_md)
            except Exception as exc:  # noqa: BLE001
                logger.warning("camofox-search: registering skill %s failed: %s",
                               child.name, exc)
