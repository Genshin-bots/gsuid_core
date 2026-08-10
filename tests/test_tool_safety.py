"""工具执行失败回收：单工具抛错不得炸整轮 Agent。"""

from __future__ import annotations

import asyncio

from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.test import TestModel
from pydantic_ai_skills.exceptions import SkillNotFoundError

from gsuid_core.ai_core.tool_safety import (
    format_tool_execute_error,
    build_tool_safety_capability,
)


def test_format_tool_execute_error_shape() -> None:
    msg = format_tool_execute_error("read_skill_resource", SkillNotFoundError("Skill 'data-analysis' not found."))
    assert msg.startswith("⚠️ 工具 read_skill_resource 执行失败")
    assert "SkillNotFoundError" in msg
    assert "data-analysis" in msg
    assert "换工具" in msg


def test_format_truncates_long_detail() -> None:
    long = "x" * 2000
    msg = format_tool_execute_error("t", RuntimeError(long))
    assert len(msg) < 1200
    assert "…" in msg


def test_tool_safety_capability_id() -> None:
    cap = build_tool_safety_capability()
    assert cap.id == "gscore-tool-safety"


def _boom() -> str:
    """故意失败的测试工具。"""
    raise SkillNotFoundError("Skill 'data-analysis' not found.")


def test_agent_tool_exception_becomes_tool_return() -> None:
    """挂 tool_safety 后，工具体 raise 应变为 tool return，Agent 可继续产出文本。"""
    agent = Agent(
        model=TestModel(),
        tools=[_boom],
        capabilities=[build_tool_safety_capability()],
    )
    result = asyncio.run(agent.run("go"))
    out = str(result.output)
    assert "data-analysis" in out
    assert "执行失败" in out

    return_texts: list[str] = []
    for m in result.all_messages():
        for p in m.parts:
            if isinstance(p, ToolReturnPart):
                return_texts.append(str(p.content))
    assert any("data-analysis" in t and "执行失败" in t for t in return_texts)


def test_agent_without_safety_raises() -> None:
    """对照：无 capability 时同工具异常会冒泡。"""
    agent = Agent(model=TestModel(), tools=[_boom])
    raised = False
    try:
        asyncio.run(agent.run("go"))
    except SkillNotFoundError:
        raised = True
    assert raised
