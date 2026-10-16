"""Configuration resolution for camofox-search.

Values resolve config-aware, in this order:

1. the Hermes config/env layer (``hermes_cli.config.get_env_value``) — which is
   secret-scope aware, so a routed profile in a multiplexed gateway reads its
   own ``.env`` and never another profile's credentials;
2. the process environment (standalone runs / tests outside Hermes).

An ``UnscopedSecretError`` raised by the Hermes layer is deliberately re-raised
(same policy as ``agent.web_search_provider.get_provider_env``): a scoped miss
must refuse, not silently fall back to the launch profile's credentials.
"""

from __future__ import annotations

import os

DEFAULT_BASE_URL = "http://localhost:9377"
DEFAULT_ENGINE = "bing"
DEFAULT_USER_ID = "camofox_search"
DEFAULT_TIMEOUT_SECONDS = 45.0


def _env_value(name: str) -> str:
    """Resolve ``name`` through the Hermes config layer, then the process env."""
    try:
        from hermes_cli.config import get_env_value
    except Exception:  # pragma: no cover - Hermes import path unavailable
        get_env_value = None  # type: ignore[assignment]
    if get_env_value is not None:
        try:
            val = get_env_value(name)
        except Exception as exc:
            try:
                from agent.secret_scope import UnscopedSecretError
            except Exception:
                UnscopedSecretError = ()  # type: ignore[assignment,misc]
            if isinstance(exc, UnscopedSecretError):
                raise
            val = None
        if val:
            return str(val).strip()
    return (os.environ.get(name, "") or "").strip()


def camofox_base_url() -> str:
    """``CAMOFOX_URL`` with the conventional local default, trailing slash stripped."""
    return (_env_value("CAMOFOX_URL") or DEFAULT_BASE_URL).rstrip("/")


def camofox_api_key() -> str:
    """``CAMOFOX_API_KEY`` (empty string when the server runs unauthenticated)."""
    return _env_value("CAMOFOX_API_KEY")


def camofox_configured() -> bool:
    """True when CAMOFOX_URL or CAMOFOX_API_KEY is explicitly configured.

    Cheap, no network: this is the provider's ``is_available()`` gate.
    """
    return bool(_env_value("CAMOFOX_URL") or _env_value("CAMOFOX_API_KEY"))


def camofox_timeout() -> float:
    """HTTP timeout for Camofox calls: ``CAMOFOX_SEARCH_TIMEOUT`` seconds."""
    raw = _env_value("CAMOFOX_SEARCH_TIMEOUT")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return val if val > 0 else DEFAULT_TIMEOUT_SECONDS


def camofox_user_id() -> str:
    """Camofox user/profile id the plugin browses with.

    One stable id (:data:`DEFAULT_USER_ID`, override ``CAMOFOX_SEARCH_USER_ID``)
    keeps the browser profile persistent, so cookies imported by
    ``camofox-search-import-cookies`` apply to every search and extraction.
    """
    return (_env_value("CAMOFOX_SEARCH_USER_ID") or DEFAULT_USER_ID).strip()


def default_engine() -> str:
    """Search engine id: ``CAMOFOX_SEARCH_ENGINE`` (default ``bing``), lowercased."""
    return (_env_value("CAMOFOX_SEARCH_ENGINE") or DEFAULT_ENGINE).strip().lower()
