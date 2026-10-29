"""web_search 配图条目格式化（供 research→render 嵌图）。"""

from __future__ import annotations

from gsuid_core.ai_core.buildin_tools.web_search import _format_results_for_model


def test_format_includes_image_urls() -> None:
    results = [
        {
            "title": "某新闻",
            "url": "https://example.com/a",
            "content": "摘要文字",
            "score": 0.9,
        },
        {
            "title": "(配图)",
            "url": "https://cdn.example.com/hero.jpg",
            "content": "",
            "score": 0.0,
            "image_url": "https://cdn.example.com/hero.jpg",
            "kind": "image",
        },
    ]
    text = _format_results_for_model(results, query="某新闻")
    assert "配图" in text
    assert "https://cdn.example.com/hero.jpg" in text
    assert "某新闻" in text
    assert "query: 某新闻" in text
    assert "image_url" in text or "嵌图" in text or "配图" in text


def test_format_empty() -> None:
    assert "没有搜到" in _format_results_for_model([])


def test_format_prints_page_date_when_present() -> None:
    text = _format_results_for_model(
        [
            {
                "title": "T",
                "url": "https://e.com",
                "content": "body",
                "score": 0.1,
                "published_date": "2026-03-01",
            },
            {
                "title": "U",
                "url": "https://u.com",
                "content": "b",
                "score": 0.1,
                "published_date": "",
            },
        ]
    )
    assert "published: 2026-03-01" in text
    assert text.count("published:") == 1


def test_format_normalizes_iso_and_drops_dirty_date() -> None:
    text = _format_results_for_model(
        [
            {
                "title": "T",
                "url": "https://e.com",
                "content": "body",
                "score": 0.1,
                "published_date": "2026-06-01T00:00:00+00:00",
            },
            {
                "title": "U",
                "url": "https://u.com",
                "content": "b",
                "score": 0.1,
                "published_date": "unknown",
            },
        ]
    )
    assert "published: 2026-06-01" in text
    assert "unknown" not in text
    assert text.count("published:") == 1


def test_format_disclaimer_is_generic() -> None:
    text = _format_results_for_model([{"title": "T", "url": "https://e.com", "content": "body", "score": 0.1}])
    assert "时效存疑" not in text
    assert "市价" not in text
    assert "结果页默认值" in text
    assert "天气" not in text
    assert "合肥" not in text
    assert "点位" not in text
    assert "报价" not in text
    assert "仅供参考" in text or "外部资料" in text
