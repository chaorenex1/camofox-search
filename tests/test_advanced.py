"""Offline tests for advanced.py — engine catalog + advanced URL construction."""

from __future__ import annotations

import pytest

from plugin.camofox_search.advanced import (
    ADVANCED_ENGINES,
    GLOBAL_VERTICALS,
    AdvancedOptionError,
    UnknownAdvancedEngineError,
    build_engine_url,
    compose_query,
    engine_catalog,
    get_advanced_engine,
)


def _build(engine_id: str, query: str = "python test", **options):
    return build_engine_url(get_advanced_engine(engine_id), query, **options)


# ---- catalog lookup ------------------------------------------------------


def test_lookup_by_id_alias_and_domain():
    assert get_advanced_engine("bing-cn").id == "bing-cn"
    assert get_advanced_engine("DDG").id == "duckduckgo"
    assert get_advanced_engine("360").id == "so360"
    assert get_advanced_engine("weixin").id == "sogou-wechat"
    assert get_advanced_engine("wechat").id == "sogou-wechat"
    # domain forms: www dropped, subdomains map to their canonical entry
    assert get_advanced_engine("www.bing.com").id == "bing"
    assert get_advanced_engine("cn.bing.com").id == "bing-cn"
    assert get_advanced_engine("https://search.brave.com/search?q=x").id == "brave"
    assert get_advanced_engine("google.com.hk").id == "google-hk"


def test_unknown_engine_lists_valid_ids():
    with pytest.raises(UnknownAdvancedEngineError) as excinfo:
        get_advanced_engine("yandex")
    assert "yandex" in str(excinfo.value)
    assert "bing-cn" in str(excinfo.value)


def test_engine_catalog_rows_sorted_and_complete():
    rows = engine_catalog()
    ids = [row["id"] for row in rows]
    assert ids == sorted(ADVANCED_ENGINES)
    assert {row["id"] for row in rows} == set(ADVANCED_ENGINES)
    for row in rows:
        assert row["name"] and row["domain"]


# ---- plain URLs per engine ----------------------------------------------


def test_minimal_urls_per_engine():
    expected = {
        "baidu": "https://www.baidu.com/s?wd=python+test",
        "bing": "https://www.bing.com/search?q=python+test",
        "bing-cn": "https://cn.bing.com/search?q=python+test&ensearch=0",
        "bing-int": "https://cn.bing.com/search?q=python+test&ensearch=1",
        "so360": "https://www.so.com/s?q=python+test",
        "sogou": "https://www.sogou.com/web?query=python+test",
        "sogou-wechat": "https://wx.sogou.com/weixin?type=2&query=python+test",
        "shenma": "https://m.sm.cn/s?q=python+test",
        "google": "https://www.google.com/search?q=python+test",
        "google-hk": "https://www.google.com.hk/search?q=python+test",
        "duckduckgo": "https://duckduckgo.com/?q=python+test&ia=web",
        "yahoo": "https://search.yahoo.com/search?p=python+test",
        "startpage": "https://www.startpage.com/sp/search?query=python+test",
        "brave": "https://search.brave.com/search?q=python+test",
        "ecosia": "https://www.ecosia.org/search?q=python+test",
        "qwant": "https://www.qwant.com/?q=python+test",
        "wolframalpha": "https://www.wolframalpha.com/input?i=python+test",
    }
    for engine_id, url in expected.items():
        built = _build(engine_id)
        assert built.url == url, engine_id
        assert built.applied == {} and built.unsupported == [], engine_id


def test_unicode_query_is_percent_encoded():
    built = _build("baidu", "深度学习")
    assert built.url == (
        "https://www.baidu.com/s?wd=%E6%B7%B1%E5%BA%A6%E5%AD%A6%E4%B9%A0")


# ---- operators -----------------------------------------------------------


def test_operator_fields_compose_into_the_query():
    built = _build(
        "bing", "python",
        site="zhihu.com", filetype=".PDF",
        exact=["machine learning", 'say "hi"'],
        exclude=["jobs", "-spam"],
        inurl="login", intitle=["index"], intext="password")
    assert built.query == (
        'python site:zhihu.com filetype:pdf inurl:login intitle:index '
        'intext:password "machine learning" "say hi" -jobs -spam')
    assert "q=python+site%3Azhihu.com+filetype%3Apdf" in built.url
    assert "%22machine+learning%22" in built.url
    assert "+-jobs+-spam" in built.url


def test_compose_query_keeps_inline_operators():
    assert compose_query("site:github.com python -snake OR rust") == \
        "site:github.com python -snake OR rust"
    assert compose_query("hello", site="a.com") == "hello site:a.com"
    with pytest.raises(AdvancedOptionError):
        compose_query("   ")


# ---- time ----------------------------------------------------------------


def test_time_preset_mapping_per_engine():
    assert _build("google", time="week").url.endswith("q=python+test&tbs=qdr%3Aw")
    assert _build("google-hk", time="hour").url.endswith("&tbs=qdr%3Ah")
    assert _build("bing-cn", time="day").url.endswith('&filters=ex1%3A%22ez1%22')
    assert _build("bing", time="month").url.endswith('&filters=ex1%3A%22ez3%22')
    assert _build("duckduckgo", time="year").url.endswith("&df=y")
    assert _build("brave", time="week").url.endswith("&tf=pw")
    assert _build("startpage", time="month").url.endswith("&time=month")


def test_time_unsupported_is_reported_not_invented():
    built = _build("bing-cn", time="hour")  # Bing has no hour preset
    assert built.applied == {}
    assert built.unsupported == ["time"]
    assert "filters" not in built.url

    built = _build("baidu", time="week")
    assert built.unsupported == ["time"]
    assert built.url == "https://www.baidu.com/s?wd=python+test"


def test_time_custom_range_google_and_ddg():
    built = _build("google", time="2024-01-01..2024-12-31")
    assert "tbs=cdr%3A1%2Ccd_min%3A1%2F1%2F2024%2Ccd_max%3A12%2F31%2F2024" in built.url
    assert built.applied["time"] == "2024-01-01..2024-12-31"

    built = _build("duckduckgo", time="2024-01-01..2024-06-30")
    assert "df=2024-01-01..2024-06-30" in built.url

    # engines without a custom-range mapping omit it
    built = _build("brave", time="2024-01-01..2024-12-31")
    assert built.unsupported == ["time"]


def test_time_validation_errors():
    for bad in ("2024-13-01..2024-12-31", "2024-07-01..2024-06-30", "not-a-time",
                "2024-01-01"):
        with pytest.raises(AdvancedOptionError):
            _build("google", time=bad)


# ---- language / region ---------------------------------------------------


def test_language_and_region_mapping():
    built = _build("google", language="zh-CN", region="cn")
    assert "hl=zh-CN" in built.url and "lr=lang_zh-CN" in built.url
    assert "gl=cn" in built.url
    assert built.applied == {"language": "zh-CN", "region": "cn"}

    assert _build("bing-cn", language="en").url.endswith("&setlang=en")
    assert _build("duckduckgo", region="us-en").url.endswith("&kl=us-en")


def test_language_region_unsupported_reported():
    built = _build("baidu", language="zh-CN", region="cn")
    assert built.unsupported == ["language", "region"]
    assert "hl=" not in built.url and "kl=" not in built.url


# ---- verticals -----------------------------------------------------------


def test_vertical_parameter_mapping():
    assert "tbm=nws" in _build("google", vertical="news").url
    assert "tbm=isch" in _build("google", vertical="images").url
    assert _build("duckduckgo", vertical="images").url.endswith("&ia=images")
    assert _build("brave", vertical="news").url.endswith("&source=news")


def test_vertical_url_overrides():
    assert _build("google", vertical="maps").url == (
        "https://www.google.com/maps/search/python+test")
    assert _build("google", vertical="scholar", query="x").url == (
        "https://scholar.google.com/scholar?q=x")
    assert _build("baidu", vertical="news").url == (
        "https://news.baidu.com/ns?word=python+test")
    assert _build("baidu", vertical="academic").url == (
        "https://xueshu.baidu.com/s?wd=python+test")
    assert _build("bing", vertical="news").url == (
        "https://www.bing.com/news/search?q=python+test")


def test_vertical_override_still_applies_engine_wide_options():
    built = _build("bing-cn", vertical="academic", time="month")
    assert built.url == ("https://cn.bing.com/academic/search?q=python+test"
                         '&filters=ex1%3A%22ez3%22')
    assert built.applied == {"vertical": "academic", "time": "month"}


def test_vertical_web_is_a_no_op_and_unsupported_is_reported():
    built = _build("google", vertical="web")
    assert built.url == "https://www.google.com/search?q=python+test"
    assert built.unsupported == []

    built = _build("ecosia", vertical="news")
    assert built.unsupported == ["vertical"]
    assert "tbm" not in built.url

    # global vocabulary the catalog does not map anywhere
    built = _build("bing-cn", vertical="shopping")
    assert built.unsupported == ["vertical"]


# ---- pagination / limit --------------------------------------------------


def test_pagination_mapping_per_engine():
    assert _build("google", page=2).url.endswith("&start=10")
    assert _build("google", page=3).url.endswith("&start=20")
    assert _build("bing-cn", page=2).url.endswith("&first=11")
    assert _build("bing-cn", page=3).url.endswith("&first=21")
    assert _build("baidu", page=2).url.endswith("&pn=10")
    assert _build("sogou", page=4).url.endswith("&page=4")
    assert _build("yahoo", page=2).url.endswith("&b=11")


def test_page_one_omits_the_parameter():
    assert _build("google", page=1).url == "https://www.google.com/search?q=python+test"


def test_pagination_unsupported_reported():
    built = _build("duckduckgo", page=2)
    assert built.unsupported == ["page"]
    assert built.applied == {}


def test_limit_mapping_and_clamping():
    assert _build("google", limit=10).url.endswith("&num=10")
    assert _build("bing", limit=7).url.endswith("&count=7")
    assert _build("google", limit=999).url.endswith("&num=20")
    # engines without a size parameter simply drop it (not "unsupported")
    built = _build("baidu", limit=10)
    assert built.url == "https://www.baidu.com/s?wd=python+test"
    assert built.unsupported == []


# ---- validation ----------------------------------------------------------


def test_page_and_limit_validation_errors():
    with pytest.raises(AdvancedOptionError):
        _build("google", page=0)
    with pytest.raises(AdvancedOptionError):
        _build("google", page="first")
    with pytest.raises(AdvancedOptionError):
        _build("google", limit="lots")


def test_applied_and_unsupported_report_the_full_option_set():
    built = _build("google", "q", time="week", language="en", region="us",
                   vertical="news", page=2, limit=8)
    assert built.applied == {
        "vertical": "news", "time": "week", "language": "en",
        "region": "us", "page": 2, "limit": 8}
    assert built.unsupported == []


def test_global_vertical_vocabulary():
    assert GLOBAL_VERTICALS == (
        "web", "news", "images", "videos", "maps", "shopping", "books",
        "academic", "scholar")
