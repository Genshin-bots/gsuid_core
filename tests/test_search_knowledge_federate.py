"""search_knowledge 联邦 FileOS：分源返回 + 无 owner fail-closed。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _ctx(user_id: str | None = "u1", group_id: str | None = "g1") -> Any:
    ev = None
    if user_id is not None:
        ev = SimpleNamespace(user_id=user_id, group_id=group_id, session_id="s1")
    deps = SimpleNamespace(ev=ev, parent_session_id=None, bot=None)
    return SimpleNamespace(deps=deps)


def test_search_knowledge_federates_fileos_and_kb() -> None:
    from gsuid_core.ai_core.buildin_tools.rag_search import search_knowledge

    kb_point = SimpleNamespace(payload={"title": "kb doc", "text": "stable"}, score=0.9)

    async def _fake_fileos(ctx: Any, query: str, **kwargs: Any) -> str:
        return "【近期检索落盘】融合命中 1 条：\n- to_abc | web_search_tool | 某摘要"

    with (
        patch(
            "gsuid_core.ai_core.buildin_tools.rag_search.query_knowledge",
            new=AsyncMock(return_value=[kb_point]),
        ),
        patch(
            "gsuid_core.ai_core.planning.tool_output_tools.search_fileos_outputs",
            new=_fake_fileos,
        ),
        patch(
            "gsuid_core.ai_core.register.handle_tool_result",
            new=AsyncMock(side_effect=lambda bot, raw: raw),
        ),
    ):
        out = _run(search_knowledge(_ctx(), query="测试主题"))

    assert "【知识库】" in out
    assert "kb doc" in out or "stable" in out
    assert "【近期检索落盘】" in out
    assert "to_abc" in out
    assert "read_handle" in out or "过时" in out


def test_search_knowledge_empty_both_sources() -> None:
    from gsuid_core.ai_core.buildin_tools.rag_search import search_knowledge

    with (
        patch(
            "gsuid_core.ai_core.buildin_tools.rag_search.query_knowledge",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "gsuid_core.ai_core.planning.tool_output_tools.search_fileos_outputs",
            new=AsyncMock(return_value=""),
        ),
        patch(
            "gsuid_core.ai_core.register.handle_tool_result",
            new=AsyncMock(side_effect=lambda bot, raw: raw),
        ),
    ):
        out = _run(search_knowledge(_ctx(), query="空"))

    assert "【知识库】" in out
    assert "未找到" in out
    assert "【近期检索落盘】" in out
