"""Keenable web search: public title header, keyed endpoint, result mapping."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from gsuid_core.ai_core.web_search.keenable_search import (
    APP_TITLE,
    KEYED_SEARCH_URL,
    PUBLIC_SEARCH_URL,
    keenable_search,
    _normalize_items,
    keenable_search_configured,
)


def test_keenable_is_always_considered_configured():
    assert keenable_search_configured() is True


def test_normalize_items_maps_snippet_and_skips_blank_urls():
    payload = {
        "results": [
            {"title": "A", "url": "https://a.example", "snippet": "body A", "score": "0.5"},
            {"title": "Missing url"},
            {"title": "B", "link": "https://b.example", "content": "body B"},
        ]
    }
    out = _normalize_items(payload, max_results=10)
    assert out == [
        {
            "title": "A",
            "url": "https://a.example",
            "content": "body A",
            "published_date": "",
            "score": 0.5,
        },
        {
            "title": "B",
            "url": "https://b.example",
            "content": "body B",
            "published_date": "",
            "score": 0.0,
        },
    ]


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def text(self):
        return "ok"

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


def _session(response):
    session = MagicMock()
    session.post.return_value = response
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


def test_keenable_search_uses_public_endpoint_and_title_header_without_a_key():
    response = _FakeResponse({"results": [{"title": "A", "url": "https://a.example", "snippet": "body A"}]})
    session = _session(response)

    with (
        patch(
            "gsuid_core.ai_core.web_search.keenable_search._get_api_key_pool",
            return_value=[],
        ),
        patch(
            "gsuid_core.ai_core.web_search.keenable_search.aiohttp.ClientSession",
            return_value=session,
        ),
        patch(
            "gsuid_core.ai_core.web_search.keenable_search.aiohttp.ClientTimeout",
            return_value=SimpleNamespace(),
        ),
    ):
        out = asyncio.run(keenable_search("hello", max_results=5))

    assert out[0]["url"] == "https://a.example"
    kwargs = session.post.call_args.kwargs
    assert session.post.call_args.args[0] == PUBLIC_SEARCH_URL
    assert kwargs["json"] == {"query": "hello", "max_results": 5}
    assert kwargs["headers"]["X-Keenable-Title"] == APP_TITLE
    assert "X-API-Key" not in kwargs["headers"]


def test_keenable_search_uses_keyed_endpoint_when_a_key_is_configured():
    response = _FakeResponse({"results": [{"title": "A", "url": "https://a.example", "snippet": "body A"}]})
    session = _session(response)

    with (
        patch(
            "gsuid_core.ai_core.web_search.keenable_search._get_api_key_pool",
            return_value=["secret-key"],
        ),
        patch(
            "gsuid_core.ai_core.web_search.keenable_search.aiohttp.ClientSession",
            return_value=session,
        ),
        patch(
            "gsuid_core.ai_core.web_search.keenable_search.aiohttp.ClientTimeout",
            return_value=SimpleNamespace(),
        ),
    ):
        asyncio.run(keenable_search("hello", max_results=3))

    kwargs = session.post.call_args.kwargs
    assert session.post.call_args.args[0] == KEYED_SEARCH_URL
    assert kwargs["headers"]["X-API-Key"] == "secret-key"
    assert kwargs["headers"]["X-Keenable-Title"] == APP_TITLE
