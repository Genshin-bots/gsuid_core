"""handle_tool_result：默认不全量砍 4k，避免 FileOS 真身半截落盘。"""

from __future__ import annotations

import asyncio
from typing import TypeVar
from collections.abc import Coroutine

from gsuid_core.ai_core.utils import handle_tool_result

T = TypeVar("T")


def _run(coro: Coroutine[object, object, T]) -> T:
    return asyncio.run(coro)


def test_default_keeps_long_tool_body() -> None:
    body = "x" * 15_000
    out = _run(handle_tool_result(None, body))
    assert out == body
    assert "系统截断" not in out


def test_explicit_small_max_still_truncates() -> None:
    body = "y" * 5000
    out = _run(handle_tool_result(None, body, max_length=4000))
    assert out.startswith("y" * 4000)
    assert "系统截断" in out
    assert "1000" in out


def test_paginated_window_bypasses_cap() -> None:
    head = "【读窗口】offset=0 limit=8000 total=9999\n"
    body = head + ("z" * 200)
    out = _run(handle_tool_result(None, body, max_length=100))
    assert out == body
    assert "系统截断" not in out
