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
    text = _format_results_for_model(results)
    assert "配图" in text
    assert "https://cdn.example.com/hero.jpg" in text
    assert "某新闻" in text
    assert "image_url" in text or "嵌图" in text or "配图" in text


def test_format_empty() -> None:
    assert "没有搜到" in _format_results_for_model([])


def test_format_disclaimer_is_generic() -> None:
    text = _format_results_for_model([{"title": "T", "url": "https://e.com", "content": "body", "score": 0.1}])
    assert "时效存疑" not in text
    assert "市价" not in text
    assert "点位" not in text
    assert "报价" not in text
    assert "仅供参考" in text or "外部资料" in text
