"""Offline tests for the search-engine definitions."""

from __future__ import annotations

import pytest

from plugin.camofox_search.engines import ENGINES, UnknownEngineError, get_engine


def test_get_engine_is_case_insensitive_and_strips():
    assert get_engine("Bing").name == "bing"
    assert get_engine(" google ").name == "google"
    assert get_engine("DuckDuckGo").name == "duckduckgo"


def test_get_engine_unknown_lists_valid_engines():
    with pytest.raises(UnknownEngineError, match="bing"):
        get_engine("yandex")


def test_all_engines_have_url_builder_and_extractor():
    for name, engine in ENGINES.items():
        url = engine.build_url("test query", 5)
        assert url.startswith("https://"), name
        js = engine.extract_js(5)
        assert f"engine:'{name}'" in js, name
        assert "slice(0,5)" in js, name


def test_bing_url_encodes_query_and_clamps_count():
    engine = get_engine("bing")
    url = engine.build_url("hermes agent", 5)
    assert "bing.com/search?q=hermes+agent" in url
    assert "count=10" in url  # clamped up to the minimum page size
    assert "count=30" in engine.build_url("x", 999)  # clamped down


def test_google_url_encodes_query():
    url = get_engine("google").build_url("café & bar", 5)
    assert "google.com/search?q=caf%C3%A9+%26+bar" in url
    assert "num=10" in url


def test_duckduckgo_url():
    url = get_engine("duckduckgo").build_url("hello world", 5)
    assert "duckduckgo.com/?q=hello+world" in url


def test_bing_extractor_selectors():
    js = get_engine("bing").extract_js(7)
    assert "#b_results > li.b_algo" in js
    assert "h2 a" in js
    assert "slice(0,7)" in js


def test_extractor_limit_is_int_formatted():
    js = get_engine("bing").extract_js(3)
    assert "slice(0,3)" in js
