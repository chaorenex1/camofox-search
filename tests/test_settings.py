"""Offline tests for configuration resolution (settings.py).

``_env_value`` is stubbed (it resolves through Hermes' config layer first in
production) so the engine-list parsing is tested deterministically.
"""

from __future__ import annotations

from plugin.camofox_search import settings


def _configure(monkeypatch, value: str) -> None:
    monkeypatch.setattr(settings, "_env_value", lambda name: value)


def test_default_engine_list_is_duckduckgo_google_bing(monkeypatch):
    _configure(monkeypatch, "")
    assert settings.default_engines() == ["duckduckgo", "google", "bing"]


def test_default_engines_parses_whitespace_and_commas(monkeypatch):
    _configure(monkeypatch, " Bing,GOOGLE   duckduckgo ")
    assert settings.default_engines() == ["bing", "google", "duckduckgo"]


def test_default_engines_lowercases_dedupes_and_drops_blanks(monkeypatch):
    _configure(monkeypatch, " , Bing bing google , ")
    assert settings.default_engines() == ["bing", "google"]


def test_blank_configuration_falls_back_to_default_list(monkeypatch):
    _configure(monkeypatch, "  ,  ")
    assert settings.default_engines() == ["duckduckgo", "google", "bing"]


def test_blank_configuration_falls_back_for_primary_engine(monkeypatch):
    _configure(monkeypatch, None)
    assert settings.default_engine() == "duckduckgo"


def test_default_engine_is_the_first_configured(monkeypatch):
    _configure(monkeypatch, "google bing")
    assert settings.default_engine() == "google"
