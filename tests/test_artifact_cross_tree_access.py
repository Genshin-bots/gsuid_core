"""跨叶子树 artifact_get 放行策略（调研→render 接力）。

测试用 asyncio.run 包装（不依赖 pytest-asyncio）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from pydantic_ai import RunContext

from gsuid_core.ai_core.planning.models import AIAgentArtifact
from gsuid_core.ai_core.planning.runtime import PlanRunContext
from gsuid_core.ai_core.planning.kanban_tools import (
    extract_res_ids,
    _artifact_access_allowed,
)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_extract_res_ids_unique_order() -> None:
    text = "先看 res_aaaaaaaaaaaa 再渲 res_bbbbbbbbbbbb，重复 res_aaaaaaaaaaaa"
    assert extract_res_ids(text) == ["res_aaaaaaaaaaaa", "res_bbbbbbbbbbbb"]
    assert extract_res_ids("") == []
    assert extract_res_ids("no handles") == []


def _art(root: str = "root_src", art_id: str = "res_deadbeefcafe") -> AIAgentArtifact:
    return cast(
        AIAgentArtifact,
        SimpleNamespace(id=art_id, root_task_id=root),
    )


def _task(
    *,
    task_id: str = "task_cur",
    owner: str = "u1",
    session: str = "sess_a",
    scope: str = "user:u1",
    goal: str = "",
    inputs: list[str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        owner_user_id=owner,
        session_id=session,
        scope_key=scope,
        goal=goal,
        input_artifact_ids=inputs or [],
    )


def _ctx(user_id: str | None = None) -> RunContext[Any]:
    ev = SimpleNamespace(user_id=user_id) if user_id is not None else None
    return cast(RunContext[Any], SimpleNamespace(deps=SimpleNamespace(ev=ev)))


def test_same_tree_allowed() -> None:
    plan = PlanRunContext(task_id="t1", root_task_id="root_a")
    art = _art(root="root_a")

    async def _go() -> bool:
        return await _artifact_access_allowed(art=art, plan_ctx=plan, ctx=_ctx())

    assert _run(_go()) is True


def test_cross_tree_same_owner_session() -> None:
    plan = PlanRunContext(task_id="t_render", root_task_id="root_render")
    art = _art(root="root_research", art_id="res_deadbeefcafe")
    current = _task(task_id="t_render", owner="u1", session="sess_a")
    source = _task(task_id="root_research", owner="u1", session="sess_a")

    async def _go() -> bool:
        with patch(
            "gsuid_core.ai_core.planning.kanban_tools.AIAgentTask.get_by_id",
            new=AsyncMock(side_effect=lambda tid: current if tid == "t_render" else source),
        ):
            return await _artifact_access_allowed(art=art, plan_ctx=plan, ctx=_ctx())

    assert _run(_go()) is True


def test_cross_tree_different_owner_denied() -> None:
    plan = PlanRunContext(task_id="t_render", root_task_id="root_render")
    art = _art(root="root_research")
    current = _task(task_id="t_render", owner="u1", session="sess_a")
    source = _task(task_id="root_research", owner="u2", session="sess_a")

    async def _go() -> bool:
        with patch(
            "gsuid_core.ai_core.planning.kanban_tools.AIAgentTask.get_by_id",
            new=AsyncMock(side_effect=lambda tid: current if tid == "t_render" else source),
        ):
            return await _artifact_access_allowed(art=art, plan_ctx=plan, ctx=_ctx())

    assert _run(_go()) is False


def test_cross_tree_explicit_goal_reference() -> None:
    plan = PlanRunContext(task_id="t_render", root_task_id="root_render")
    art = _art(root="root_other", art_id="res_deadbeefcafe")
    current = _task(
        task_id="t_render",
        owner="u1",
        session="sess_a",
        goal="请 artifact_get('res_deadbeefcafe') 后出图",
    )
    source = _task(task_id="root_other", owner="u9", session="sess_x")

    async def _go() -> bool:
        with patch(
            "gsuid_core.ai_core.planning.kanban_tools.AIAgentTask.get_by_id",
            new=AsyncMock(side_effect=lambda tid: current if tid == "t_render" else source),
        ):
            return await _artifact_access_allowed(art=art, plan_ctx=plan, ctx=_ctx())

    assert _run(_go()) is True


def test_cross_tree_input_artifact_ids() -> None:
    plan = PlanRunContext(task_id="t_render", root_task_id="root_render")
    art = _art(root="root_other", art_id="res_deadbeefcafe")
    current = _task(task_id="t_render", owner="u1", inputs=["res_deadbeefcafe"])
    source = _task(task_id="root_other", owner="u9")

    async def _go() -> bool:
        with patch(
            "gsuid_core.ai_core.planning.kanban_tools.AIAgentTask.get_by_id",
            new=AsyncMock(side_effect=lambda tid: current if tid == "t_render" else source),
        ):
            return await _artifact_access_allowed(art=art, plan_ctx=plan, ctx=_ctx())

    assert _run(_go()) is True


def test_main_persona_owner_check() -> None:
    art = _art(root="root_src")
    source = _task(task_id="root_src", owner="u1")

    async def _go_ok() -> bool:
        with patch(
            "gsuid_core.ai_core.planning.kanban_tools.AIAgentTask.get_by_id",
            new=AsyncMock(return_value=source),
        ):
            return await _artifact_access_allowed(art=art, plan_ctx=None, ctx=_ctx("u1"))

    async def _go_bad() -> bool:
        with patch(
            "gsuid_core.ai_core.planning.kanban_tools.AIAgentTask.get_by_id",
            new=AsyncMock(return_value=source),
        ):
            return await _artifact_access_allowed(art=art, plan_ctx=None, ctx=_ctx("u2"))

    assert _run(_go_ok()) is True
    assert _run(_go_bad()) is False
