"""Firecrawl web 搜索：SDK 归一化、调度接入。"""

from __future__ import annotations

import pytest
from firecrawl.v2.types import Document, SearchData, SearchResultWeb, DocumentMetadata, SearchResultImages
from firecrawl.v2.utils.error_handler import FirecrawlError

from gsuid_core.ai_core.web_search import firecrawl_search as firecrawl_mod
from gsuid_core.ai_core.configs.ai_config import AI_CONFIG
from gsuid_core.ai_core.web_search.search import (
    _DEFAULT_PROVIDER_ORDER,
    _invoke_provider,
    _provider_configured,
)
from gsuid_core.utils.plugins_config.models import GsStrConfig, GsListStrConfig


class _Cfg:
    def __init__(self, mapping: dict[str, object]) -> None:
        self._mapping = mapping

    def get_config(self, key: str, default: object | None = None) -> object:
        if key in self._mapping:
            return type("_Field", (), {"data": self._mapping[key]})()
        if default is not None:
            return type("_Field", (), {"data": default})()
        return type("_Field", (), {"data": ""})()


def test_firecrawl_is_websearch_option() -> None:
    provider = AI_CONFIG["websearch_provider"]
    fallback = AI_CONFIG["websearch_fallback_order"]
    assert isinstance(provider, GsStrConfig)
    assert isinstance(fallback, GsListStrConfig)
    assert "Firecrawl" in provider.options
    assert "Firecrawl" in fallback.options


def test_firecrawl_in_default_order_and_configured() -> None:
    assert "Firecrawl" in _DEFAULT_PROVIDER_ORDER
    assert _provider_configured("Firecrawl") is True
    assert firecrawl_mod.firecrawl_search_configured() is True


def test_clamp_max_results() -> None:
    assert firecrawl_mod._clamp_max_results(0) == 1
    assert firecrawl_mod._clamp_max_results(10) == 10
    assert firecrawl_mod._clamp_max_results(100) == 100
    assert firecrawl_mod._clamp_max_results(101) == 100


def test_normalize_search_data_web() -> None:
    payload = SearchData(
        web=[
            SearchResultWeb(url="https://www.firecrawl.dev/", title="Firecrawl", description="Web data API"),
            SearchResultWeb(url="", title="", description=""),
        ]
    )
    hits = firecrawl_mod._normalize_results(payload, max_results=10)
    assert len(hits) == 1
    assert hits[0]["title"] == "Firecrawl"
    url = hits[0]["url"]
    assert isinstance(url, str)
    assert url.startswith("https://")
    assert hits[0]["content"] == "Web data API"
    assert hits[0]["score"] == 0.0


def test_normalize_document_and_dict_envelope() -> None:
    doc = Document(
        markdown="# Hello",
        metadata=DocumentMetadata(title="Example", url="https://example.com"),
    )
    payload = SearchData(web=[doc])
    hits = firecrawl_mod._normalize_results(payload, max_results=10)
    assert hits[0]["title"] == "Example"
    assert hits[0]["url"] == "https://example.com"
    assert hits[0]["content"] == "# Hello"

    raw = {
        "web": [
            {"title": "A", "url": "https://a.example", "description": "d"},
            {"title": "", "url": "", "description": ""},
        ]
    }
    hits2 = firecrawl_mod._normalize_results(raw, max_results=10)
    assert len(hits2) == 1
    assert hits2[0]["content"] == "d"


def test_normalize_images_capped() -> None:
    payload = SearchData(
        web=[SearchResultWeb(url="https://w.example", title="W", description="web")],
        images=[
            SearchResultImages(title="i1", image_url="https://img.example/1.png", url="https://page.example"),
            SearchResultImages(title="i2", image_url="https://img.example/2.png"),
        ],
    )
    hits = firecrawl_mod._normalize_results(payload, max_results=10)
    assert hits[0]["title"] == "W"
    kinds = [str(h["kind"]) for h in hits if "kind" in h]
    assert kinds == ["image", "image"]
    assert hits[1]["image_url"] == "https://img.example/1.png"


class _FakeClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.kwargs = kwargs
        captured.append(kwargs)

    async def search(self, query: str, limit: int, timeout: int) -> object:
        calls.append({"query": query, "limit": limit, "timeout": timeout, "client": self.kwargs})
        if fail_with is not None:
            raise fail_with
        return SearchData(web=[SearchResultWeb(url="https://t.example", title=query, description="c")])


captured: list[dict[str, object]] = []
calls: list[dict[str, object]] = []
fail_with: Exception | None = None


@pytest.fixture(autouse=True)
def _reset_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    captured.clear()
    calls.clear()
    global fail_with
    fail_with = None
    monkeypatch.setattr(firecrawl_mod, "AsyncFirecrawl", _FakeClient)
    monkeypatch.setattr(
        firecrawl_mod,
        "firecrawl_config",
        _Cfg({"api_key": [], "max_results": 10, "timeout": 30}),
    )


@pytest.mark.anyio
async def test_keyless_constructs_without_api_key() -> None:
    hits = await firecrawl_mod.firecrawl_search("hello", max_results=1)
    assert hits[0]["title"] == "hello"
    assert captured
    assert "api_key" not in captured[0]
    assert calls[0]["limit"] == 1
    assert calls[0]["timeout"] == 30000


@pytest.mark.anyio
async def test_keyed_constructs_with_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        firecrawl_mod,
        "firecrawl_config",
        _Cfg({"api_key": ["fc-testkey"], "max_results": 10, "timeout": 15}),
    )
    await firecrawl_mod.firecrawl_search("hello")
    assert captured[0]["api_key"] == "fc-testkey"
    assert calls[0]["timeout"] == 15000


@pytest.mark.anyio
async def test_sdk_error_raises() -> None:
    global fail_with
    fail_with = FirecrawlError("unauthorized", status_code=401)
    with pytest.raises(firecrawl_mod.FirecrawlSearchError, match="unauthorized"):
        await firecrawl_mod.firecrawl_search("hello")


@pytest.mark.anyio
async def test_invoke_provider_routes_firecrawl(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(*, query: str, max_results: int | None = None) -> list[dict[str, str | float]]:
        return [{"title": query, "url": "https://x.example", "content": "c", "score": 0.0}]

    monkeypatch.setattr(
        "gsuid_core.ai_core.web_search.search.firecrawl_search",
        _fake,
    )
    raw = await _invoke_provider("Firecrawl", "quantum", 3, with_context=False)
    assert isinstance(raw, list)
    assert raw[0]["title"] == "quantum"
    wrapped = await _invoke_provider("Firecrawl", "quantum", 3, with_context=True)
    assert isinstance(wrapped, dict)
    assert wrapped["answer"] is None
    results = wrapped["results"]
    assert isinstance(results, list)
    assert results[0]["url"] == "https://x.example"
