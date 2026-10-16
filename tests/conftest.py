"""Shared test setup: repo import path + the Hermes provider contract + rule-store isolation.

Tests run fully offline. When the real Hermes runtime is importable (running
under the Hermes venv python) its ABC is used; otherwise a faithful stub of
``agent.web_search_provider.WebSearchProvider`` is installed so the plugin can
be imported and exercised in isolation. The stub mirrors the members the plugin
touches: ``name``, ``display_name``, ``is_available``, ``supports_*``,
``get_setup_schema``.

Every test's rule database is redirected to a temporary directory via
``CAMOFOX_SEARCH_DATA_DIR`` so no test ever reads or writes a real HERMES_HOME.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolated_rule_data_dir(tmp_path_factory, monkeypatch):
    """Point the camofox-search rule store at a per-test temp directory."""
    data_dir = tmp_path_factory.mktemp("camofox-search-data")
    monkeypatch.setenv("CAMOFOX_SEARCH_DATA_DIR", str(data_dir))
    return data_dir


class _StubProviderBase:
    @property
    def name(self) -> str:
        raise NotImplementedError

    @property
    def display_name(self) -> str:
        return self.name

    def get_setup_schema(self):
        return {"name": self.display_name, "badge": "", "tag": "", "env_vars": []}


class _StubWebSearchProvider(_StubProviderBase):
    def is_available(self) -> bool:
        raise NotImplementedError

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query, limit=5):
        raise NotImplementedError

    def extract(self, urls, **kwargs):
        raise NotImplementedError


def _ensure_web_search_provider_contract() -> None:
    try:
        importlib.import_module("agent.web_search_provider")
        return
    except Exception:
        pass

    agent_pkg = types.ModuleType("agent")
    agent_pkg.__path__ = []  # type: ignore[attr-defined]

    provider_base = types.ModuleType("agent.provider_base")
    provider_base.ProviderBase = _StubProviderBase  # type: ignore[attr-defined]

    web_search = types.ModuleType("agent.web_search_provider")
    web_search.WebSearchProvider = _StubWebSearchProvider  # type: ignore[attr-defined]

    agent_pkg.provider_base = provider_base  # type: ignore[attr-defined]
    agent_pkg.web_search_provider = web_search  # type: ignore[attr-defined]

    sys.modules["agent"] = agent_pkg
    sys.modules["agent.provider_base"] = provider_base
    sys.modules["agent.web_search_provider"] = web_search


_ensure_web_search_provider_contract()
