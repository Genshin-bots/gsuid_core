"""模型 HTTP 池的库归属与超时契约。

openai 3.x / anthropic 1.x 的 http_client 形参只收 httpx2 客户端（与 httpx 不同类、
不可互传），google-genai 仍吃 httpx。池子一旦串了库或丢了 §23 墙钟超时，静态检查
看不出后者，只有这里会红。
"""

from __future__ import annotations

import httpx
import httpx2

from gsuid_core.ai_core.configs.models import (
    _shared_model_http_client,
    _shared_model_httpx2_client,
)

WALL_CLOCK = (15.0, 180.0, 60.0, 30.0)


def test_pools_use_the_library_their_sdk_takes() -> None:
    for provider in ("openai", "anthropic"):
        v2 = _shared_model_httpx2_client(provider)
        assert isinstance(v2, httpx2.AsyncClient)
        assert not isinstance(v2, httpx.AsyncClient)
        assert (v2.timeout.connect, v2.timeout.read, v2.timeout.write, v2.timeout.pool) == WALL_CLOCK

    gemini = _shared_model_http_client("gemini")
    assert isinstance(gemini, httpx.AsyncClient)
    assert not isinstance(gemini, httpx2.AsyncClient)
    assert (gemini.timeout.connect, gemini.timeout.read, gemini.timeout.write, gemini.timeout.pool) == WALL_CLOCK


def test_pools_are_shared_per_provider() -> None:
    """评审修复 F8：每次建模型新开客户端会累积不关闭的连接池。"""
    assert _shared_model_httpx2_client("openai") is _shared_model_httpx2_client("openai")
    assert _shared_model_http_client("gemini") is _shared_model_http_client("gemini")
