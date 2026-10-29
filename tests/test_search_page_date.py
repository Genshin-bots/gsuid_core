"""搜索结果的页面日期：来源有就复制，没有就留空。"""

from __future__ import annotations

from gsuid_core.ai_core.web_search import jina_search as jina_mod
from gsuid_core.ai_core.web_search.search import _parse_mcp_search_result
from gsuid_core.ai_core.web_search.date_utils import normalize_published_date


def test_jina_copies_published_time() -> None:
    hits = jina_mod._normalize_items(
        {
            "data": [
                {
                    "title": "T",
                    "url": "https://j.example",
                    "content": "c",
                    "publishedTime": "2024-04-01",
                }
            ]
        },
        10,
    )
    assert hits[0]["published_date"] == "2024-04-01"
    missing = jina_mod._normalize_items(
        {"data": [{"title": "T", "url": "https://j.example", "content": "c"}]},
        10,
    )
    assert missing[0]["published_date"] == ""


def test_mcp_copies_page_date() -> None:
    raw = (
        '{"results":['
        '{"title":"T","url":"https://m.example","snippet":"s","date":"2024-08-01"},'
        '{"title":"U","url":"https://u.example","snippet":"s"}'
        "]}"
    )
    hits = _parse_mcp_search_result(raw)
    assert hits[0]["published_date"] == "2024-08-01"
    assert hits[1]["published_date"] == ""


def test_normalize_published_date_formats() -> None:
    assert normalize_published_date("2026-06-01T00:00:00+00:00") == "2026-06-01"
    assert normalize_published_date("2024-05-01T12:30:45Z") == "2024-05-01"
    assert normalize_published_date(" 2026-01-02 ") == "2026-01-02"
    assert normalize_published_date("2020/3/5") == "2020-03-05"
    assert normalize_published_date("Wed, 01 May 2024 10:00:00 GMT") == "2024-05-01"


def test_normalize_published_date_drops_dirty() -> None:
    assert normalize_published_date(None) == ""
    assert normalize_published_date("") == ""
    assert normalize_published_date("null") == ""
    assert normalize_published_date("unknown") == ""
    assert normalize_published_date("0000-00-00") == ""
    assert normalize_published_date("not a date") == ""
