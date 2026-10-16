"""camofox-search — a Hermes web-search provider backed by the local Camofox browser.

Serviced capabilities: ``web_search`` and ``web_extract`` (selected via
``web.search_backend`` / ``web.extract_backend`` / ``web.backend``).
"""

from __future__ import annotations

from .provider import CamofoxSearchProvider

__all__ = ["CamofoxSearchProvider", "register"]


def register(ctx) -> None:
    """Hermes plugin entry point: register the web-search provider."""
    ctx.register_web_search_provider(CamofoxSearchProvider())
