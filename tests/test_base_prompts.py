import asyncio

import pytest

from gsuid_core.ai_core.base_prompts import FRAMEWORK_CORE_CONSTRAINTS, join_framework_constraints
from gsuid_core.ai_core.agent_node.models import PLAIN_SESSION_CONSTRAINTS, AgentNode, compose_plain_session_prompt
from gsuid_core.ai_core.persona.processor import build_persona_prompt
from gsuid_core.ai_core.prompt_group.catalog import default_body
from gsuid_core.ai_core.scheduled_task.executor import SCHEDULED_TASK_EXECUTOR_PROMPT


def test_framework_core_is_prompt_group_default() -> None:
    """提示词组「框架底线」的默认正文就是这份常量。"""
    assert default_body("framework_core") == FRAMEWORK_CORE_CONSTRAINTS


def test_scheduled_task_prompt_keeps_executor_rules() -> None:
    """执行体自己的规则不把底线再抄一遍；拼提示时才附上。"""
    assert "你是定时任务执行器（不是角色扮演）" in SCHEDULED_TASK_EXECUTOR_PROMPT
    assert "条件不满足时只输出 <SILENCE>" in SCHEDULED_TASK_EXECUTOR_PROMPT
    assert FRAMEWORK_CORE_CONSTRAINTS not in SCHEDULED_TASK_EXECUTOR_PROMPT
    joined = join_framework_constraints(None, SCHEDULED_TASK_EXECUTOR_PROMPT)
    assert joined.count("【框架通用底线】") == 1


def test_plain_session_joins_framework_from_group() -> None:
    """Plain 入口附上默认组的框架底线，并保留只读汇总规则。"""
    node = AgentNode(node_id="plain_demo", display_name="演示", prompt="只回答问题。")
    text = compose_plain_session_prompt(node)
    assert "只查不写" in PLAIN_SESSION_CONSTRAINTS
    assert FRAMEWORK_CORE_CONSTRAINTS in text
    assert text.count("【框架通用底线】") == 1


def test_join_framework_uses_group_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """自定义组的底线替换默认正文。留空则不附。已包含同一段时不重复附。"""

    def _render(_persona: str | None, key: str, **_values: str) -> str:
        assert key == "framework_core"
        return "组别底线"

    monkeypatch.setattr("gsuid_core.ai_core.prompt_group.store.render_prompt", _render)
    assert join_framework_constraints("某人格", "入口规则") == "入口规则\n\n组别底线"
    assert join_framework_constraints("某人格", "入口规则\n\n组别底线") == "入口规则\n\n组别底线"

    monkeypatch.setattr(
        "gsuid_core.ai_core.prompt_group.store.render_prompt",
        lambda _persona, key, **_values: "  ",
    )
    assert join_framework_constraints("某人格", "入口规则") == "入口规则"


def test_build_persona_prompt_injects_framework_core(monkeypatch: pytest.MonkeyPatch) -> None:
    """主会话 persona 提示词组装时附上提示词组里的框架底线。"""
    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.processor.load_persona",
        lambda _name: asyncio.sleep(0, result="Name: 测试角色"),
    )
    prompt = asyncio.run(build_persona_prompt("测试角色"))
    assert FRAMEWORK_CORE_CONSTRAINTS in prompt
    assert prompt.count("【框架通用底线】") == 1
