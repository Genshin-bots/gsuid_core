"""AnySearch web 搜索：REST 归一化、请求体、调度接入。"""

from __future__ import annotations

import json

import pytest

from gsuid_core.ai_core.web_search import search as search_mod, anysearch_search as anysearch_mod
from gsuid_core.ai_core.configs.ai_config import AI_CONFIG
from gsuid_core.ai_core.web_search.search import (
    _DEFAULT_PROVIDER,
    _DEFAULT_PROVIDER_ORDER,
    _get_provider,
    _invoke_provider,
    _provider_configured,
)
from gsuid_core.utils.plugins_config.models import GsStrConfig, GsListStrConfig


class _Field:
    def __init__(self, data: object) -> None:
        self.data = data


class _Cfg:
    def __init__(self, mapping: dict[str, object]) -> None:
        self._mapping = mapping

    def get_config(self, key: str, default: object | None = None) -> _Field:
        if key in self._mapping:
            return _Field(self._mapping[key])
        if default is not None:
            return _Field(default)
        return _Field("")


def test_anysearch_is_websearch_option() -> None:
    provider = AI_CONFIG["websearch_provider"]
    fallback = AI_CONFIG["websearch_fallback_order"]
    assert isinstance(provider, GsStrConfig)
    assert isinstance(fallback, GsListStrConfig)
    assert provider.data == "AnySearch"
    assert provider.options[0] == "AnySearch"
    assert "AnySearch" in fallback.options


def test_anysearch_is_default_and_auto_collect() -> None:
    assert _DEFAULT_PROVIDER == "AnySearch"
    assert _DEFAULT_PROVIDER_ORDER[0] == "AnySearch"
    assert _provider_configured("AnySearch") is True


def test_empty_or_unconfigured_primary_uses_anysearch(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Box:
        def __init__(self, data: object) -> None:
            self.data = data

    orig = search_mod.ai_config.get_config

    def _fake_get(key: str, default: object | None = None) -> object:
        if key == "websearch_provider":
            return _Box("")
        if default is not None:
            return orig(key, default)
        return orig(key)

    monkeypatch.setattr(search_mod.ai_config, "get_config", _fake_get)
    assert _get_provider() == "AnySearch"

    def _fake_tavily(key: str, default: object | None = None) -> object:
        if key == "websearch_provider":
            return _Box("Tavily")
        if default is not None:
            return orig(key, default)
        return orig(key)

    monkeypatch.setattr(search_mod.ai_config, "get_config", _fake_tavily)
    monkeypatch.setattr(search_mod, "_provider_configured", lambda name: name == "AnySearch")
    assert _get_provider() == "AnySearch"


def test_clamp_max_results() -> None:
    assert anysearch_mod._clamp_max_results(0) == 1
    assert anysearch_mod._clamp_max_results(10) == 10
    assert anysearch_mod._clamp_max_results(100) == 100
    assert anysearch_mod._clamp_max_results(101) == 100


def test_normalize_official_rest_envelope() -> None:
    payload = {
        "code": 0,
        "message": "success",
        "request_id": "rid-1",
        "data": {
            "results": [
                {
                    "title": "Hello, world - Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/Hello,_world",
                    "snippet": "A short summary",
                    "content": "Cleaned-up body",
                },
                {"title": "", "url": "", "snippet": "", "content": ""},
                "skip-me",
            ],
            "metadata": {"total_results": 1, "search_time_ms": 12},
        },
    }
    hits = anysearch_mod._normalize_results(payload, max_results=10)
    assert len(hits) == 1
    assert hits[0]["title"] == "Hello, world - Wikipedia"
    url = hits[0]["url"]
    assert isinstance(url, str)
    assert url.startswith("https://")
    assert hits[0]["content"] == "Cleaned-up body"
    assert hits[0]["score"] == 0.0


def test_normalize_prefers_content_then_snippet() -> None:
    payload = {
        "data": {
            "results": [
                {"title": "A", "url": "https://a.example", "snippet": "s"},
                {"title": "B", "url": "https://b.example", "content": "c", "snippet": "s"},
            ]
        }
    }
    hits = anysearch_mod._normalize_results(payload, max_results=1)
    assert len(hits) == 1
    assert hits[0]["content"] == "s"
    hits2 = anysearch_mod._normalize_results(payload, max_results=10)
    assert hits2[1]["content"] == "c"


def test_build_search_body_omits_empty_enums(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        anysearch_mod,
        "anysearch_config",
        _Cfg({"zone": "", "language": "de"}),
    )
    body = anysearch_mod._build_search_body("quantum computing", 10)
    assert body == {"query": "quantum computing", "max_results": 10}


def test_build_search_body_includes_zone_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        anysearch_mod,
        "anysearch_config",
        _Cfg({"zone": "cn", "language": "zh-CN"}),
    )
    body = anysearch_mod._build_search_body("q", 5)
    assert body == {"query": "q", "max_results": 5, "zone": "cn", "language": "zh-CN"}


class _FakeResp:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self._body = json.dumps(payload) if not isinstance(payload, str) else payload

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _FakeResp:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _patch_session(
    monkeypatch: pytest.MonkeyPatch,
    resp: _FakeResp,
    captured: list[dict[str, object]],
) -> None:
    class _FakeSession:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *args: object) -> bool:
            return False

        def post(self, url: str, json: object = None, headers: object = None) -> _FakeResp:
            captured.append({"url": url, "json": json, "headers": headers})
            return resp

    monkeypatch.setattr(anysearch_mod.aiohttp, "ClientSession", _FakeSession)


@pytest.mark.anyio
async def test_anonymous_request_omits_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    payload = {
        "code": 0,
        "data": {"results": [{"title": "T", "url": "https://t.example", "content": "c"}]},
    }
    _patch_session(monkeypatch, _FakeResp(200, payload), captured)
    monkeypatch.setattr(
        anysearch_mod,
        "anysearch_config",
        _Cfg({"api_key": [], "max_results": 10, "timeout": 30, "zone": "", "language": ""}),
    )
    hits = await anysearch_mod.anysearch_search("hello", max_results=1)
    assert hits[0]["title"] == "T"
    assert captured
    headers = captured[0]["headers"]
    assert isinstance(headers, dict)
    assert "Authorization" not in headers
    assert headers["X-Anysearch-Client"] == "gsuid-core/1.0"
    assert captured[0]["url"] == "https://api.anysearch.com/v1/search"


@pytest.mark.anyio
async def test_keyed_request_sends_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []
    payload = {"code": 0, "data": {"results": [{"title": "T", "url": "u", "content": "c"}]}}
    _patch_session(monkeypatch, _FakeResp(200, payload), captured)
    monkeypatch.setattr(
        anysearch_mod,
        "anysearch_config",
        _Cfg(
            {
                "api_key": ["as_sk_testkey"],
                "max_results": 10,
                "timeout": 30,
                "zone": "",
                "language": "",
            }
        ),
    )
    await anysearch_mod.anysearch_search("hello")
    headers = captured[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer as_sk_testkey"


@pytest.mark.anyio
async def test_http_401_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []
    _patch_session(
        monkeypatch,
        _FakeResp(401, {"code": -1, "message": "invalid_api_key", "request_id": "r1"}),
        captured,
    )
    monkeypatch.setattr(
        anysearch_mod,
        "anysearch_config",
        _Cfg({"api_key": ["bad"], "max_results": 10, "timeout": 30, "zone": "", "language": ""}),
    )
    with pytest.raises(anysearch_mod.AnySearchError, match="401"):
        await anysearch_mod.anysearch_search("hello")


@pytest.mark.anyio
async def test_invoke_provider_routes_anysearch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(*, query: str, max_results: int | None = None) -> list[dict[str, str | float]]:
        return [{"title": query, "url": "https://x.example", "content": "c", "score": 0.0}]

    monkeypatch.setattr(
        "gsuid_core.ai_core.web_search.search.anysearch_search",
        _fake,
    )
    raw = await _invoke_provider("AnySearch", "quantum", 3, with_context=False)
    assert isinstance(raw, list)
    assert raw[0]["title"] == "quantum"
    wrapped = await _invoke_provider("AnySearch", "quantum", 3, with_context=True)
    assert isinstance(wrapped, dict)
    assert wrapped["answer"] is None
    results = wrapped["results"]
    assert isinstance(results, list)
    assert results[0]["url"] == "https://x.example"
