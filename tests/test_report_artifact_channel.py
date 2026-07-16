"""OOC 制品化两通道回归测试（plans/prod_session_review §1/§3/§4）。

2026-07-16 生产事故：persona 主 Agent 把整篇研报（📊 标题 + 表格）当角色台词输出，
人格漂移严重；定时任务执行体注入 persona 后无视任务里的静默条款，向群里播报非事件。

修复面与测试对应：
- ``_extract_report_blocks``：``<report>`` 制品块与角色台词分离（发送端解析）；
- ``_report_footer``：制品图片统一免责/数据时点脚注（§3 合规垫层，不依赖用户偏好）；
- ``SYSTEM_CONSTRAINTS``：persona prompt 教学输出契约；
- ``execute_scheduled_task``：中性执行体（不注入 persona）+ 静默闸 + 溯源尾注。
"""

from typing import Any
from datetime import datetime, timedelta

import pytest

from gsuid_core.ai_core.utils import SILENCE_MARKERS, _report_footer, _extract_report_blocks

# ─────────────────────────────────────────────
# _extract_report_blocks
# ─────────────────────────────────────────────


def test_report_block_separated_from_persona_speech() -> None:
    text = (
        '唔…帮你看完了…别追高…\n\n<report title="XX股份 速览">数据截至 7/15 收盘\n\n'
        "| 指标 | 数值 |\n|---|---|\n| 营收 | +12% |</report>"
    )
    speech, reports = _extract_report_blocks(text)
    assert "别追高" in speech
    assert "<report" not in speech
    assert "营收" not in speech
    assert len(reports) == 1
    title, body = reports[0]
    assert title == "XX股份 速览"
    assert "| 指标 | 数值 |" in body
    assert "数据截至 7/15 收盘" in body


def test_report_block_without_title() -> None:
    speech, reports = _extract_report_blocks("<report>正文内容</report>")
    assert speech == ""
    assert reports == [("", "正文内容")]


def test_multiple_report_blocks_preserved_in_order() -> None:
    text = '开场白\n<report title="A">甲</report>中场\n<report title="B">乙</report>'
    speech, reports = _extract_report_blocks(text)
    assert [t for t, _ in reports] == ["A", "B"]
    assert "开场白" in speech
    assert "中场" in speech


def test_unclosed_report_tag_left_in_text() -> None:
    """未闭合标签（截断输出）不匹配：内容留在正文，走既有长 markdown 出图兜底，不丢内容。"""
    text = '<report title="X">被截断的内容…'
    speech, reports = _extract_report_blocks(text)
    assert reports == []
    assert "被截断的内容" in speech


def test_empty_report_body_dropped() -> None:
    speech, reports = _extract_report_blocks("台词<report>   </report>")
    assert reports == []
    assert speech == "台词"


def test_case_insensitive_tag() -> None:
    _, reports = _extract_report_blocks("<REPORT>内容</REPORT>")
    assert len(reports) == 1


def test_plain_text_untouched() -> None:
    speech, reports = _extract_report_blocks("纯闲聊台词，没有制品块")
    assert speech == "纯闲聊台词，没有制品块"
    assert reports == []


# ─────────────────────────────────────────────
# _report_footer（§3 合规垫层）
# ─────────────────────────────────────────────


def test_footer_contains_disclaimer_and_staleness() -> None:
    footer = _report_footer()
    assert "仅供参考" in footer
    assert "滞后" in footer
    assert "不构成" in footer


# ─────────────────────────────────────────────
# persona prompt 输出契约
# ─────────────────────────────────────────────


def test_system_constraints_teach_report_contract() -> None:
    from gsuid_core.ai_core.persona.prompts import SYSTEM_CONSTRAINTS

    assert "<report" in SYSTEM_CONSTRAINTS
    assert "重信息输出契约" in SYSTEM_CONSTRAINTS
    # 契约必须点名"表格/标题当台词=出戏"，否则模型没有理由改变行为
    assert "出戏" in SYSTEM_CONSTRAINTS


# ─────────────────────────────────────────────
# execute_scheduled_task：中性执行体 + 静默闸 + 溯源尾注
# ─────────────────────────────────────────────


class _StubLogger:
    _file_path = "stub.json"

    def close(self) -> None:
        pass


class _StubAgent:
    def __init__(self, output: str) -> None:
        self._output = output
        self._session_logger = _StubLogger()
        self.run_kwargs: dict = {}

    async def run(self, **kwargs) -> str:
        self.run_kwargs = kwargs
        return self._output


def _make_task(**overrides) -> Any:
    from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

    fields = {
        "task_id": "scheduled_task_test01",
        "task_type": "once",
        "user_id": "514971204",
        "group_id": "914411529",
        "bot_self_id": "bot",
        "user_type": "group",
        "WS_BOT_ID": None,
        "persona_name": "早柚",
        "session_id": "s",
        "trigger_time": datetime.now() - timedelta(minutes=1),
        "task_prompt": "检查价格；不满足条件时静默",
        "status": "pending",
        "bot_id": "onebot",
    }
    fields.update(overrides)
    return AIScheduledTask(**fields)


@pytest.fixture
def executor_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    """把 executor 的外部依赖全部替换为可观测桩：DB / gss / agent / emitter / 统计。"""
    import gsuid_core.gss as gss_mod
    import gsuid_core.ai_core.gs_agent as gs_agent_mod
    import gsuid_core.ai_core.proactive as proactive_mod
    import gsuid_core.ai_core.scheduled_task.executor as executor_mod
    from gsuid_core.ai_core.statistics import manager as stats_mod

    env: dict = {"emits": [], "updates": [], "agent": _StubAgent("默认结果"), "create_agent_kwargs": {}}

    task_holder: dict = {"task": None}

    async def fake_select_rows(**kwargs) -> list:
        return [task_holder["task"]]

    async def fake_update(select_data, update_data):
        env["updates"].append((select_data, update_data))

    monkeypatch.setattr(executor_mod.AIScheduledTask, "select_rows", fake_select_rows)
    monkeypatch.setattr(executor_mod.AIScheduledTask, "update_data_by_data", fake_update)

    def fake_create_agent(**kwargs):
        env["create_agent_kwargs"] = kwargs
        return env["agent"]

    monkeypatch.setattr(gs_agent_mod, "create_agent", fake_create_agent)

    async def fake_emit(**kwargs):
        env["emits"].append(kwargs)
        return True

    monkeypatch.setattr(proactive_mod, "emit_proactive_message", fake_emit)
    monkeypatch.setattr(stats_mod.statistics_manager, "record_trigger", lambda **k: None)
    monkeypatch.setattr(gss_mod.gss, "active_bot", {})

    env["set_task"] = lambda t: task_holder.__setitem__("task", t)
    return env


@pytest.mark.anyio
async def test_executor_uses_neutral_prompt_not_persona(executor_env: dict) -> None:
    """执行体必须用中性 prompt，不注入 persona（§4："发不发"在无人格上下文中判定）。"""
    from gsuid_core.ai_core.scheduled_task.executor import (
        SCHEDULED_TASK_EXECUTOR_PROMPT,
        execute_scheduled_task,
    )

    executor_env["set_task"](_make_task())
    await execute_scheduled_task("scheduled_task_test01")

    kwargs = executor_env["create_agent_kwargs"]
    assert kwargs["system_prompt"] == SCHEDULED_TASK_EXECUTOR_PROMPT
    assert kwargs["persona_name"] is None
    # 中性 prompt 本身必须写明静默硬约束
    assert "<SILENCE>" in SCHEDULED_TASK_EXECUTOR_PROMPT
    assert "不扮演角色" in SCHEDULED_TASK_EXECUTOR_PROMPT


@pytest.mark.anyio
async def test_executor_silences_non_event(executor_env: dict) -> None:
    """执行体输出 <SILENCE> 时不得向群里播报（§4 事故的直接防线）。"""
    from gsuid_core.ai_core.scheduled_task.executor import execute_scheduled_task

    executor_env["agent"] = _StubAgent("<SILENCE>")
    executor_env["set_task"](_make_task())
    await execute_scheduled_task("scheduled_task_test01")

    assert executor_env["emits"] == []
    # 任务本身仍正常结转（once → executed）
    assert any("status" in u[1] and u[1]["status"] == "executed" for u in executor_env["updates"])


@pytest.mark.anyio
async def test_executor_appends_provenance_footer(executor_env: dict) -> None:
    """播报消息必须带溯源尾注（§5"这是谁要的提醒"事故）。"""
    from gsuid_core.ai_core.scheduled_task.executor import execute_scheduled_task

    executor_env["agent"] = _StubAgent("巨化 600160 现价 40.2，已到 MA60 警示位。")
    executor_env["set_task"](_make_task())
    await execute_scheduled_task("scheduled_task_test01")

    assert len(executor_env["emits"]) == 1
    message = executor_env["emits"][0]["message"]
    assert "scheduled_task_test01" in message
    assert "定时任务" in message
    assert message.startswith("巨化 600160")


def test_silence_marker_membership() -> None:
    """执行体的静默闸依赖 <SILENCE> 在 SILENCE_MARKERS 中，防止常量漂移。"""
    assert "<SILENCE>" in SILENCE_MARKERS
