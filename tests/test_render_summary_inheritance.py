"""测试渲染出图工具对上游产物摘要的通用继承。

纯离线测试，所有 ID 与上下文均已脱敏。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from gsuid_core.ai_core.planning.runtime import PlanRunContext
from gsuid_core.ai_core.buildin_tools.html_render_tools import _summary_for_render


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_summary_for_render_inherits_upstream_artifact_summary() -> None:
    plan = PlanRunContext(task_id="task_render_anon_01", root_task_id="root_anon_01")
    render_task = SimpleNamespace(
        id="task_render_anon_01",
        display_name="通用业绩指标概览卡",
        goal="把 res_data_mock_111 这份业务数据渲染为图片卡片",
        input_artifact_ids=["res_data_mock_111"],
    )
    upstream_artifact = SimpleNamespace(
        id="res_data_mock_111",
        summary="通用指标事实清单：项目A营收100亿(+5%)，利润20亿(-10%)",
    )

    async def _go() -> str:
        with (
            patch(
                "gsuid_core.ai_core.planning.runtime.get_plan_context",
                return_value=plan,
            ),
            patch(
                "gsuid_core.ai_core.planning.models.AIAgentTask.get_by_id",
                new=AsyncMock(return_value=render_task),
            ),
            patch(
                "gsuid_core.ai_core.planning.models.AIAgentArtifact.get_by_id",
                new=AsyncMock(return_value=upstream_artifact),
            ),
        ):
            return await _summary_for_render("render output")

    res = _run(_go())
    assert "通用业绩指标概览卡" in res
    assert "依据：" in res
    assert "通用指标事实清单：项目A营收100亿(+5%)，利润20亿(-10%)" in res


def test_summary_for_render_fallback_to_goal_when_no_upstream() -> None:
    plan = PlanRunContext(task_id="task_render_anon_02", root_task_id="root_anon_02")
    render_task = SimpleNamespace(
        id="task_render_anon_02",
        display_name="",
        goal="生成一张纯装饰性欢迎页面",
        input_artifact_ids=[],
    )

    async def _go() -> str:
        with (
            patch(
                "gsuid_core.ai_core.planning.runtime.get_plan_context",
                return_value=plan,
            ),
            patch(
                "gsuid_core.ai_core.planning.models.AIAgentTask.get_by_id",
                new=AsyncMock(return_value=render_task),
            ),
        ):
            return await _summary_for_render("render output")

    res = _run(_go())
    assert res == "生成一张纯装饰性欢迎页面"
