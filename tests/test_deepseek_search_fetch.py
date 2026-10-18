"""DeepSeek web 搜索与网页抓取：REST 归一化、请求体、调度接入与多源策略。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from gsuid_core.ai_core.web_fetch import (
    _fetch_via_deepseek,
    _normalize_fetch_provider,
    fetch_webpage_as_markdown,
)
from gsuid_core.ai_core.web_search import (
    search as search_mod,
    web_search,
    deepseek_search as deepseek_mod,
)
from gsuid_core.ai_core.configs.ai_config import AI_CONFIG, deepseek_config
from gsuid_core.ai_core.web_search.search import (
    _DEFAULT_PROVIDER_ORDER,
    _provider_configured,
)
from gsuid_core.utils.plugins_config.models import GsStrConfig, GsListStrConfig


class _Box:
    def __init__(self, data: object) -> None:
        self.data = data


class _FakeConfig:
    def __init__(self, mapping: dict[str, object]) -> None:
        self._mapping = mapping

    def get_config(self, key: str, default: object | None = None) -> _Box:
        if key in self._mapping:
            return _Box(self._mapping[key])
        if default is not None:
            return _Box(default)
        return _Box("")


def test_deepseek_is_websearch_and_webfetch_option() -> None:
    search_provider = AI_CONFIG["websearch_provider"]
    search_fallback = AI_CONFIG["websearch_fallback_order"]
    fetch_provider = AI_CONFIG["webfetch_provider"]
    fetch_fallback = AI_CONFIG["webfetch_fallback_order"]

    assert isinstance(search_provider, GsStrConfig)
    assert isinstance(search_fallback, GsListStrConfig)
    assert "DeepSeek" in search_provider.options
    assert "DeepSeek" in search_fallback.options

    assert isinstance(fetch_provider, GsStrConfig)
    assert isinstance(fetch_fallback, GsListStrConfig)
    assert "DeepSeek" in fetch_provider.options
    assert "DeepSeek" in fetch_fallback.options


def test_deepseek_in_default_order_and_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "DeepSeek" in _DEFAULT_PROVIDER_ORDER

    fake_empty = _FakeConfig({"api_key": []})
    monkeypatch.setattr(deepseek_mod, "deepseek_config", fake_empty)
    assert deepseek_mod.deepseek_search_configured() is False
    assert _provider_configured("DeepSeek") is False

    fake_with_key = _FakeConfig({"api_key": ["sk-test-123"]})
    monkeypatch.setattr(deepseek_mod, "deepseek_config", fake_with_key)
    assert deepseek_mod.deepseek_search_configured() is True
    assert _provider_configured("DeepSeek") is True


def test_clamp_max_results_and_query() -> None:
    assert deepseek_mod._clamp_max_results(0) == 1
    assert deepseek_mod._clamp_max_results(10) == 10
    assert deepseek_mod._clamp_max_results(50) == 50
    assert deepseek_mod._clamp_max_results(51) == 50

    short_query = "DeepSeek 最新动态"
    assert deepseek_mod._clamp_query(short_query) == short_query

    long_query = "a" * 200
    assert len(deepseek_mod._clamp_query(long_query)) == 128


def test_normalize_deepseek_search_results() -> None:
    payload = {
        "results": [
            {
                "rerank_score": 2.85,
                "title": "DeepSeek API",
                "url": "https://api-docs.deepseek.com",
                "snippet": "Short snippet for UI",
                "summary": "Long model-readable summary for LLM",
                "content": "# Full Page Content\n\nDeepSeek details...",
            },
            {
                "rerank_score": "1.5",
                "title": "DeepSeek News",
                "url": "https://example.com/news",
                "snippet": "News snippet",
                "summary": "",
                "content": "",
            },
            {"title": "", "url": "", "snippet": "", "summary": "", "content": ""},
        ]
    }

    # 当 need_content=False 时，优先使用 summary
    hits_no_content = deepseek_mod._normalize_results(payload, max_results=10, need_content=False)
    assert len(hits_no_content) == 2
    assert hits_no_content[0]["title"] == "DeepSeek API"
    assert hits_no_content[0]["url"] == "https://api-docs.deepseek.com"
    assert hits_no_content[0]["content"] == "Long model-readable summary for LLM"
    assert hits_no_content[0]["score"] == 2.85
    assert hits_no_content[1]["content"] == "News snippet"
    assert hits_no_content[1]["score"] == 1.5

    # 当 need_content=True 时，优先使用 content 全文
    hits_with_content = deepseek_mod._normalize_results(payload, max_results=10, need_content=True)
    assert hits_with_content[0]["content"] == "# Full Page Content\n\nDeepSeek details..."


@pytest.mark.anyio
async def test_deepseek_search_request_and_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cfg = _FakeConfig(
        {
            "api_key": ["sk-bad-1", "sk-good-2"],
            "max_results": 5,
            "timeout": 20,
            "search_base_url": "https://api.deepseek.com/v1/tools/search",
            "need_content": False,
        }
    )
    monkeypatch.setattr(deepseek_mod, "deepseek_config", fake_cfg)

    call_count = 0

    class _MockResponse:
        def __init__(self, status: int, text_data: str) -> None:
            self.status = status
            self._text = text_data

        async def text(self) -> str:
            return self._text

        async def __aenter__(self) -> _MockResponse:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    class _MockClientSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _MockClientSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def post(self, url: str, **kwargs: Any) -> _MockResponse:
            nonlocal call_count
            call_count += 1
            headers = kwargs.get("headers", {})
            auth = headers.get("Authorization", "")
            if "sk-bad-1" in auth:
                return _MockResponse(401, '{"error": "invalid api key"}')
            return _MockResponse(
                200,
                json.dumps(
                    {
                        "results": [
                            {
                                "title": "Result from good key",
                                "url": "https://example.com/ok",
                                "summary": "Success summary",
                                "rerank_score": 2.5,
                            }
                        ]
                    }
                ),
            )

    monkeypatch.setattr(deepseek_mod.aiohttp, "ClientSession", _MockClientSession)

    results = await deepseek_mod.deepseek_search("test query", max_results=5)
    assert len(results) == 1
    assert results[0]["title"] == "Result from good key"
    assert call_count >= 1


def test_normalize_fetch_provider() -> None:
    assert _normalize_fetch_provider("DeepSeek") == "DeepSeek"
    assert _normalize_fetch_provider("deepseek") == "DeepSeek"
    assert _normalize_fetch_provider("deepseek-ai") == "DeepSeek"
    assert _normalize_fetch_provider("Jina") == "Jina"
    assert _normalize_fetch_provider("local") == "local"


@pytest.mark.anyio
async def test_deepseek_fetch_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cfg = _FakeConfig(
        {
            "api_key": ["sk-fetch-test"],
            "timeout": 15,
            "fetch_base_url": "https://api.deepseek.com/v1/tools/fetch",
        }
    )
    monkeypatch.setattr(deepseek_config, "get_config", fake_cfg.get_config)

    class _MockResponse:
        def __init__(self, status: int, text_data: str) -> None:
            self.status = status
            self._text = text_data

        async def text(self) -> str:
            return self._text

        async def __aenter__(self) -> _MockResponse:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

    class _MockClientSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _MockClientSession:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        def post(self, url: str, **kwargs: Any) -> _MockResponse:
            return _MockResponse(
                200,
                json.dumps(
                    {
                        "title": "Article Title",
                        "content": "# Article Title\n\nThis is the markdown body.",
                    }
                ),
            )

    import gsuid_core.ai_core.web_fetch as fetch_pkg

    monkeypatch.setattr(fetch_pkg.aiohttp, "ClientSession", _MockClientSession)

    res = await _fetch_via_deepseek("https://example.com/article", max_md=1000)
    assert "# Article Title" in res
    assert "markdown body" in res


@pytest.mark.anyio
async def test_web_search_multi_source_failover(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ai = _FakeConfig(
        {
            "websearch_provider": "DeepSeek",
            "websearch_lb_strategy": "error_switch",
            "websearch_fallback_order": ["AnySearch"],
        }
    )
    monkeypatch.setattr(search_mod.ai_config, "get_config", fake_ai.get_config)
    monkeypatch.setattr(search_mod, "_provider_configured", lambda p: True)

    # 模拟 DeepSeek 抛出异常，触发 fallback 到 AnySearch
    async def _fake_deepseek(query: str, max_results: int | None = None) -> list[dict[str, str | float]]:
        raise deepseek_mod.DeepSeekSearchError("DeepSeek quota exceeded")

    async def _fake_anysearch(query: str, max_results: int | None = None) -> list[dict[str, str | float]]:
        return [{"title": "Fallback AnySearch", "url": "https://anysearch.com", "content": "ok", "score": 1.0}]

    monkeypatch.setattr(search_mod, "deepseek_search", _fake_deepseek)
    monkeypatch.setattr(search_mod, "anysearch_search", _fake_anysearch)

    results = await web_search("query", max_results=5)
    assert len(results) == 1
    assert results[0]["title"] == "Fallback AnySearch"


@pytest.mark.anyio
async def test_web_fetch_multi_source_failover(monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.ai_core.web_fetch as fetch_pkg

    fake_ai = _FakeConfig(
        {
            "webfetch_provider": "DeepSeek",
            "webfetch_lb_strategy": "error_switch",
            "webfetch_fallback_order": ["local"],
        }
    )
    monkeypatch.setattr(fetch_pkg.ai_config, "get_config", fake_ai.get_config)

    async def _fake_deepseek_fetch(url: str, max_md: int) -> str:
        raise ValueError("DeepSeek rate limit")

    async def _fake_local_fetch(url: str, timeout: int | None = None, max_length: int | None = None) -> str:
        return "# Local Fallback Markdown"

    monkeypatch.setattr(fetch_pkg, "_fetch_via_deepseek", _fake_deepseek_fetch)
    monkeypatch.setattr(fetch_pkg, "_fetch_via_local", _fake_local_fetch)

    res = await fetch_webpage_as_markdown("https://example.com/test")
    assert res == "# Local Fallback Markdown"
