"""Keenable web search: config-only auth, docs-aligned normalization, failover error wrapping."""

from __future__ import annotations

import json
import asyncio
from types import TracebackType
from dataclasses import field, dataclass

import pytest
import aiohttp

from gsuid_core.ai_core.web_search import keenable_search as ks
from gsuid_core.ai_core.configs.ai_config import keenable_config
from gsuid_core.ai_core.web_search.keenable_search import (
    APP_TITLE,
    KEYED_SEARCH_URL,
    PUBLIC_SEARCH_URL,
    KeenableSearchError,
    keenable_search,
    _normalize_items,
    keenable_search_configured,
)


@dataclass
class _PostCall:
    url: str
    payload: dict[str, object]
    headers: dict[str, str]


class _FakeResponse:
    def __init__(self, payload: object, status: int = 200, *, text_error: Exception | None = None) -> None:
        self._payload = payload
        self.status = status
        self._text_error = text_error

    async def text(self) -> str:
        if self._text_error is not None:
            raise self._text_error
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


@dataclass
class _SessionRecorder:
    responses: list[_FakeResponse]
    calls: list[_PostCall] = field(default_factory=list)

    def make_session(self, timeout: object = None) -> _FakeSession:
        return _FakeSession(self)


class _FakeSession:
    def __init__(self, recorder: _SessionRecorder) -> None:
        self._recorder = recorder

    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
    ) -> _FakeResponse:
        self._recorder.calls.append(_PostCall(url=url, payload=json, headers=headers))
        return self._recorder.responses.pop(0)

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


def test_keenable_normalize_items_and_bounds() -> None:
    assert keenable_search_configured() is True
    payload = {
        "query": "test",
        "mode": "pro",
        "results": [
            {
                "title": "A",
                "url": "https://a.example",
                "description": "",
                "snippet": "body A",
                "published_at": "2026-10-30T00:00:00Z",
                "acquired_at": "2026-11-03T08:57:22Z",
            },
            {"title": "Blank url", "url": "   ", "snippet": "ignored"},
            {"title": 123, "url": "https://b.example", "content": "body B", "score": "0.5"},
        ],
    }
    assert _normalize_items(payload, max_results=0) == []
    assert _normalize_items(payload, max_results=10) == [
        {
            "title": "A",
            "url": "https://a.example",
            "content": "body A",
            "published_date": "2026-10-30",
            "score": 0.0,
        },
        {
            "title": "123",
            "url": "https://b.example",
            "content": "body B",
            "published_date": "",
            "score": 0.5,
        },
    ]


def test_keenable_keyless_ignores_env_clamps_limits_and_wraps_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KEENABLE_API_KEY", "env-key-must-not-be-read")
    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", [])
    monkeypatch.setattr(keenable_config.get_config("mode"), "data", "realtime")

    recorder = _SessionRecorder(
        [
            _FakeResponse(
                {
                    "results": [
                        {
                            "title": "A",
                            "url": "https://a.example",
                            "snippet": "body A",
                            "published_at": "2026-11-01T12:00:00Z",
                        }
                    ]
                }
            ),
            _FakeResponse({}, text_error=aiohttp.ClientPayloadError("truncated stream")),
        ]
    )
    monkeypatch.setattr(ks.aiohttp, "ClientSession", recorder.make_session)

    out = asyncio.run(keenable_search("hello", max_results=120))
    assert out == [
        {
            "title": "A",
            "url": "https://a.example",
            "content": "body A",
            "published_date": "2026-11-01",
            "score": 0.0,
        }
    ]
    call = recorder.calls[0]
    assert call.url == PUBLIC_SEARCH_URL
    assert call.payload == {"query": "hello", "max_results": 50, "mode": "realtime"}
    assert call.headers["X-Keenable-Title"] == APP_TITLE
    assert "X-API-Key" not in call.headers

    with pytest.raises(KeenableSearchError):
        asyncio.run(keenable_search("hello", max_results=0))
    assert recorder.calls[1].payload["max_results"] == 1


def test_keenable_keyed_rotates_and_does_not_fallback_to_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", ["keen_bad", "keen_good"])
    monkeypatch.setattr(keenable_config.get_config("mode"), "data", "pro")
    monkeypatch.setattr(ks, "_select_api_key", lambda pool: pool[0] if pool else None)

    recorder = _SessionRecorder(
        [
            _FakeResponse({"error": "Invalid key", "code": "invalid_api_key"}, status=401),
            _FakeResponse({"results": [{"title": "OK", "url": "https://ok.example", "snippet": "good"}]}),
        ]
    )
    monkeypatch.setattr(ks.aiohttp, "ClientSession", recorder.make_session)

    out = asyncio.run(keenable_search("hello", max_results=3))
    assert len(out) == 1 and out[0]["url"] == "https://ok.example"
    assert [c.url for c in recorder.calls] == [KEYED_SEARCH_URL, KEYED_SEARCH_URL]
    assert [c.headers["X-API-Key"] for c in recorder.calls] == ["keen_bad", "keen_good"]

    recorder_fail = _SessionRecorder([_FakeResponse({"error": "Out of credits", "code": "insufficient_credits"}, 402)])
    monkeypatch.setattr(keenable_config.get_config("api_key"), "data", ["keen_only"])
    monkeypatch.setattr(ks.aiohttp, "ClientSession", recorder_fail.make_session)
    with pytest.raises(KeenableSearchError, match="insufficient_credits"):
        asyncio.run(keenable_search("hello", max_results=3))
    assert [c.url for c in recorder_fail.calls] == [KEYED_SEARCH_URL]
