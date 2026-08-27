"""认知层联邦检索：分源命中 + 单行空结果 + 无 owner fail-closed。

锁点变更：旧工具 `search_knowledge` 的「知识库 + FileOS 两段」已升级为 `search_cognition`
的四路联邦（记忆+偏好 / 知识 / 落盘 / 产物），检索实现随之从 `rag_search` 搬到
`cognition.facade`。断言从「两个固定段标题」升级为「统一命中列表 + kind 标签」。
兼容别名 `search_knowledge` 已删除，主人格只暴露 `search_cognition`。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from gsuid_core.ai_core.cognition import ALL_KINDS, MEMORY_KINDS, CogKind, CogScope


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _empty_profile_async(scope_key: str) -> dict[str, object]:
    return {
        "scope_key": scope_key,
        "tag_counts": {},
        "term_mappings": {},
        "member_alias_ids": {},
        "member_aliases": {},
        "last_updated": "",
    }


async def _no_canons(scope_key: str) -> list[str]:
    _ = scope_key
    return []


def _ctx(user_id: str | None = "u1", group_id: str | None = "g1") -> Any:
    """deps 用**真的** ``ToolContext`` + ``Event``：本轮去重缓存存在 ``ToolContext.extra``
    里，手搓 SimpleNamespace 会漏字段，让工具在测试里因缺 ``extra`` 而假失败。"""
    from gsuid_core.models import Event
    from gsuid_core.ai_core.models import ToolContext

    ev = None
    if user_id is not None:
        ev = Event(bot_id="HTTP", user_id=user_id, group_id=group_id)
    return SimpleNamespace(deps=ToolContext(bot=None, ev=ev))


def _kb_point(title: str, text: str, score: float = 0.9) -> Any:
    return SimpleNamespace(id="p1", payload={"id": "kb1", "title": title, "content": text}, score=score)


class _Row:
    """AIToolOutputRecord 的最小替身。"""

    def __init__(self, rid: str, summary: str) -> None:
        self.id = rid
        self.tool_name = "web_search_tool"
        self.profile = ""
        self.summary = summary
        self.date_str = "2026-08-14"


def test_cognition_federates_knowledge_and_fileos() -> None:
    from gsuid_core.ai_core.buildin_tools.rag_search import search_cognition

    with (
        patch(
            "gsuid_core.ai_core.rag.query_knowledge",
            new=AsyncMock(return_value=[_kb_point("kb doc", "stable")]),
        ),
        patch(
            "gsuid_core.ai_core.planning.tool_output_store.AIToolOutputRecord.search",
            new=AsyncMock(return_value=[_Row("to_abc", "某摘要")]),
        ),
        patch(
            "gsuid_core.ai_core.cognition.facade._search_memory",
            new=AsyncMock(return_value=([], {})),
        ),
        patch(
            "gsuid_core.ai_core.cognition.facade._search_artifacts",
            new=AsyncMock(return_value=([], {})),
        ),
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade._search_records", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade._search_images", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade._search_memes", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade._search_meme_knowledge", new=AsyncMock(return_value=([], {}))),
        patch(
            "gsuid_core.ai_core.register.handle_tool_result",
            new=AsyncMock(side_effect=lambda bot, raw: raw),
        ),
        patch("gsuid_core.ai_core.memory.group_profile.get_group_profile", new=_empty_profile_async),
        patch("gsuid_core.ai_core.cognition.nodes.AICogNode.list_world_canons_in_scope", new=_no_canons),
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade.probe_handle_alive", new=AsyncMock(return_value=True)),
    ):
        out = _run(search_cognition(_ctx(), query="测试主题"))

    assert "认知检索" in out
    assert "kb doc" in out or "stable" in out
    assert "to_abc" in out
    assert "[知识]" in out
    assert "[落盘·可能过时]" in out
    assert "read_handle" in out


def test_cognition_empty_is_single_line() -> None:
    """空结果只回一行——历史上要拼「未找到 + 无匹配 + 长说明」三大段。"""
    from gsuid_core.ai_core.buildin_tools.rag_search import search_cognition

    with (
        patch("gsuid_core.ai_core.rag.query_knowledge", new=AsyncMock(return_value=[])),
        patch(
            "gsuid_core.ai_core.planning.tool_output_store.AIToolOutputRecord.search",
            new=AsyncMock(return_value=[]),
        ),
        patch("gsuid_core.ai_core.cognition.facade._search_memory", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade._search_artifacts", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade._search_history", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade._search_records", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade._search_images", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade._search_memes", new=AsyncMock(return_value=([], {}))),
        patch("gsuid_core.ai_core.cognition.facade._search_meme_knowledge", new=AsyncMock(return_value=([], {}))),
        patch(
            "gsuid_core.ai_core.register.handle_tool_result",
            new=AsyncMock(side_effect=lambda bot, raw: raw),
        ),
        patch("gsuid_core.ai_core.memory.group_profile.get_group_profile", new=_empty_profile_async),
        patch("gsuid_core.ai_core.cognition.nodes.AICogNode.list_world_canons_in_scope", new=_no_canons),
        patch("gsuid_core.ai_core.cognition.facade._search_nodes", new=AsyncMock(return_value=([], {}))),
    ):
        out = _run(search_cognition(_ctx(), query="空"))

    assert "无命中" in out
    assert len(out.splitlines()) == 1, out
    # 上限管的是「别再拼三大段」，不是「不许指路」：一行内必须给出下一步，否则模型
    # 会原地编答案或换个说法重搜（单一动词把这类空转全压在这一个工具上）。
    assert len(out) < 160, f"空结果应 < 160 字，实际 {len(out)}"
    assert "web_search_tool" in out and "find_tools" in out, "空结果必须指路到外部/专域工具"


def test_no_owner_is_fail_closed() -> None:
    """无用户上下文时拒绝检索，绝不全局扫表。"""
    from gsuid_core.ai_core.buildin_tools.rag_search import search_cognition

    with patch(
        "gsuid_core.ai_core.register.handle_tool_result",
        new=AsyncMock(side_effect=lambda bot, raw: raw),
    ):
        out = _run(search_cognition(_ctx(user_id=None), query="x"))
    assert "无用户上下文" in out


def test_search_knowledge_tool_removed() -> None:
    import gsuid_core.ai_core.buildin_tools as tools_pkg
    import gsuid_core.ai_core.buildin_tools.rag_search as rag_search

    assert not hasattr(rag_search, "search_knowledge")
    assert "search_knowledge" not in tools_pkg.__all__


def test_private_chat_scope_never_falls_back_to_user_id() -> None:
    """私聊必须 group_id=None：回退成 user_id 会去查空的幻影 group:{user_id}。"""
    from gsuid_core.ai_core.buildin_tools.rag_search import _scope_from_ctx

    scope = _scope_from_ctx(_ctx(user_id="u1", group_id=None))
    assert scope.group_id is None and scope.is_private
    assert scope.user_id == "u1"
    group_scope = _scope_from_ctx(_ctx(user_id="u1", group_id="g1"))
    assert group_scope.group_id == "g1" and not group_scope.is_private


def test_kinds_and_scope_are_required_no_internal_default() -> None:
    """``kinds`` / ``scope`` 必填、无内部兜底——两个真实 bug 的共同根因就是可选参数被兜底。"""
    import inspect

    from gsuid_core.ai_core.cognition import search_cognition as facade

    sig = inspect.signature(facade)
    for name in ("kinds", "scope"):
        param = sig.parameters[name]
        assert param.default is inspect.Parameter.empty, f"{name} 不许有默认值"
        assert param.kind is inspect.Parameter.KEYWORD_ONLY, f"{name} 必须是关键字参数"
    # ⑧ 每轮默认切片仍只含记忆 + 偏好（延迟不回退）
    assert MEMORY_KINDS == frozenset({CogKind.EPISODE, CogKind.ENTITY, CogKind.FACT, CogKind.PREFERENCE})
    assert CogKind.KNOWLEDGE in ALL_KINDS and CogKind.KNOWLEDGE not in MEMORY_KINDS


def test_skill_doc_excluded_for_normal_users() -> None:
    """开发文档整类不对普通用户暴露，且过滤下推到后端。"""
    from gsuid_core.ai_core.cognition.facade import _search_knowledge_backend

    captured: dict[str, Any] = {}

    async def _fake_query(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    async def _empty_profile(scope_key: str) -> dict[str, object]:
        return {
            "scope_key": scope_key,
            "tag_counts": {},
            "term_mappings": {},
            "member_alias_ids": {},
            "member_aliases": {},
            "last_updated": "",
        }

    with patch("gsuid_core.ai_core.rag.query_knowledge", new=_fake_query):
        with patch("gsuid_core.ai_core.memory.group_profile.get_group_profile", new=_empty_profile):
            _run(_search_knowledge_backend("q", scope=CogScope(user_id="u1"), limit=5))
    from gsuid_core.ai_core.rag.skills_kb import SKILLS_DOC_SOURCE

    assert captured["exclude_sources"] == [SKILLS_DOC_SOURCE]
