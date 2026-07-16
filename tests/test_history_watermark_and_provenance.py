"""历史高低水位裁剪 + report 占位 + 定时任务溯源回归测试（plans/prod_session_review §5/§18/§25）。

2026-07-16 生产观察：
- auto_compact 55/58 轮触发（16~21 → 15），历史头部每轮变化 → provider 前缀缓存
  在消息段永不命中（全天命中率卡 54%）；
- 早柚自己 4.5h 前创建的盯盘任务，被问"这是谁要的提醒"时完全无法追溯——
  list_scheduled_tasks 只按提问者 user_id 过滤，别人建的任务根本查不到。

对应修复：
- ``extract_history``：超过 max_history 才裁、一次裁到低水位（0.6x），裁剪间隔内前缀稳定；
- ``_compact_report_blocks_in_history``：持久历史中 <report> 正文换占位符（省 token + 切断漂移固化）；
- ``list_scheduled_tasks`` / ``query_scheduled_task``：群聊按群列出、展示发起用户，同群成员可读。
"""

from typing import Any, Optional
from datetime import datetime

import pytest
from pydantic_ai.messages import (
    TextPart,
    ModelRequest,
    ModelResponse,
    UserPromptPart,
)

from gsuid_core.ai_core.utils import _compact_report_blocks_in_history

# ─────────────────────────────────────────────
# extract_history 高低水位
# ─────────────────────────────────────────────


def _make_agent(max_history: int) -> Any:
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

    # 只构造到能测 extract_history 的程度：绕过 __init__（避免模型/工具装配）
    agent = object.__new__(GsCoreAIAgent)
    agent.max_history = max_history

    class _NullLogger:
        def log_history_reset(self, reason: str, detail: str) -> None:
            self.last = (reason, detail)

    agent._session_logger = _NullLogger()
    return agent


def _turn(i: int) -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content=f"【用户发言】\n消息{i}")]),
        ModelResponse(parts=[TextPart(content=f"回复{i}")]),
    ]


def test_no_trim_below_watermark() -> None:
    """未超 max_history 时一条都不动——头部稳定是缓存命中的前提。"""
    agent = _make_agent(max_history=15)
    history = []
    for i in range(7):
        history.extend(_turn(i))  # 14 条
    agent.history = list(history)
    agent.extract_history()
    assert agent.history == history


def test_trim_goes_to_low_watermark_not_max() -> None:
    """超过 max_history 时一次裁到低水位（0.6x），而非"超 1 裁 1"。"""
    agent = _make_agent(max_history=15)
    history = []
    for i in range(9):
        history.extend(_turn(i))  # 18 条 > 15
    agent.history = list(history)
    agent.extract_history()
    # 低水位 = int(15*0.6) = 9；工具配对安全截断可能再少 1 条，但绝不该停在 15
    assert len(agent.history) <= 9
    assert len(agent.history) >= 7
    # 保留的是最新消息
    last = agent.history[-1]
    assert isinstance(last, ModelResponse)
    first_part = last.parts[0]
    assert isinstance(first_part, TextPart) and first_part.content == "回复8"


def test_trim_interval_gives_stable_prefix() -> None:
    """裁剪后继续追加若干轮都不再触发裁剪——这段窗口内历史头部字节稳定。"""
    agent = _make_agent(max_history=15)
    history = []
    for i in range(9):
        history.extend(_turn(i))
    agent.history = list(history)
    agent.extract_history()
    stable_head = list(agent.history)

    # 追加 2 轮（+4 条 ≤ 15）：不触发裁剪，头部对象序列不变
    agent.history.extend(_turn(100))
    agent.extract_history()
    agent.history.extend(_turn(101))
    agent.extract_history()
    assert agent.history[: len(stable_head)] == stable_head


def test_zero_max_history_clears() -> None:
    agent = _make_agent(max_history=0)
    agent.history = _turn(1)
    agent.extract_history()
    assert agent.history == []


# ─────────────────────────────────────────────
# _compact_report_blocks_in_history
# ─────────────────────────────────────────────


def test_report_body_replaced_with_placeholder() -> None:
    md = "| 指标 | 数值 |\n|---|---|\n| 营收 | +12% |"
    msg = ModelResponse(parts=[TextPart(content=f'唔…看这张…\n<report title="XX速览">{md}</report>')])
    replaced = _compact_report_blocks_in_history([msg])
    assert replaced == 1
    part = msg.parts[0]
    assert isinstance(part, TextPart)
    assert "营收" not in part.content
    assert "XX速览" in part.content  # 标题保留，后续轮可引用
    assert "唔…看这张…" in part.content  # 台词保留


def test_untitled_report_gets_generic_placeholder() -> None:
    msg = ModelResponse(parts=[TextPart(content="<report>长内容</report>")])
    _compact_report_blocks_in_history([msg])
    part = msg.parts[0]
    assert isinstance(part, TextPart)
    assert "分析资料" in part.content
    assert "长内容" not in part.content


def test_user_requests_untouched() -> None:
    msg = ModelRequest(parts=[UserPromptPart(content="<report>用户消息里的原样文本</report>")])
    replaced = _compact_report_blocks_in_history([msg])
    assert replaced == 0
    part = msg.parts[0]
    assert isinstance(part, UserPromptPart)
    assert part.content == "<report>用户消息里的原样文本</report>"


def test_plain_response_untouched() -> None:
    msg = ModelResponse(parts=[TextPart(content="普通台词，无制品块")])
    assert _compact_report_blocks_in_history([msg]) == 0


# ─────────────────────────────────────────────
# 定时任务溯源：群作用域 + 发起用户展示
# ─────────────────────────────────────────────


def _make_ctx(user_id: str, group_id: Optional[str]) -> Any:
    """按仓库测试约定构造 RunContext[ToolContext]（MagicMock 外壳 + 真实 ToolContext）。"""
    from unittest.mock import MagicMock

    from gsuid_core.ai_core.models import ToolContext

    ev = MagicMock()
    ev.user_id = user_id
    ev.group_id = group_id
    ev.session_id = "s"
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=None, ev=ev, parent_session_id="test_session")
    return ctx


def _group_task(**overrides) -> Any:
    from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

    fields = {
        "task_id": "scheduled_task_5cad21ace9f5",
        "task_type": "interval",
        "user_id": "514971204",  # Synchro 创建
        "group_id": "914411529",
        "bot_id": "onebot",
        "task_prompt": "检查巨化股份（600160）当前价格",
        "status": "pending",
        "interval_seconds": 1800,
        "max_executions": 12,
        "current_executions": 8,
        "created_at": datetime(2026, 7, 16, 10, 1),
    }
    fields.update(overrides)
    return AIScheduledTask(**fields)


@pytest.fixture
def sched_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    import gsuid_core.ai_core.buildin_tools.scheduler as sched_mod

    env = {"select_kwargs": [], "tasks": []}

    async def fake_select_rows(**kwargs) -> list:
        env["select_kwargs"].append(kwargs)
        return env["tasks"]

    monkeypatch.setattr(sched_mod.AIScheduledTask, "select_rows", fake_select_rows)
    return env


@pytest.mark.anyio
async def test_group_member_sees_others_tasks_with_creator(sched_env: dict) -> None:
    """§5 事故复现：居木（994534742）问"谁要的提醒"，必须能看到 Synchro 建的群任务。"""
    from gsuid_core.ai_core.buildin_tools.scheduler import list_scheduled_tasks

    sched_env["tasks"] = [_group_task()]
    ctx = _make_ctx(user_id="994534742", group_id="914411529")
    result = await list_scheduled_tasks(ctx)

    # 群聊 = 本群任务 ∪ 提问者自己的任务（自己私聊/它群设的提醒也要能查到，评审修复 F11）
    assert {"group_id": "914411529"} in sched_env["select_kwargs"]
    assert {"user_id": "994534742"} in sched_env["select_kwargs"]
    assert "scheduled_task_5cad21ace9f5" in result
    assert "@514971204" in result  # 发起用户以 @ 形态展示（走 at 转换，不裸出 QQ 号）


@pytest.mark.anyio
async def test_private_chat_still_filters_by_user(sched_env: dict) -> None:
    from gsuid_core.ai_core.buildin_tools.scheduler import list_scheduled_tasks

    sched_env["tasks"] = []
    ctx = _make_ctx(user_id="994534742", group_id=None)
    await list_scheduled_tasks(ctx)
    assert sched_env["select_kwargs"] == [{"user_id": "994534742"}]


@pytest.mark.anyio
async def test_query_task_readable_by_same_group_member(sched_env: dict) -> None:
    """同群成员可按尾注里的任务 ID 查详情（只读）。"""
    from gsuid_core.ai_core.buildin_tools.scheduler import query_scheduled_task

    sched_env["tasks"] = [_group_task()]
    ctx = _make_ctx(user_id="994534742", group_id="914411529")
    result = await query_scheduled_task(ctx, task_id="scheduled_task_5cad21ace9f5")
    assert "无权" not in result
    assert "514971204" in result


@pytest.mark.anyio
async def test_query_task_denied_for_outsider(sched_env: dict) -> None:
    """非发起人且不在同一群：仍然无权查看。"""
    from gsuid_core.ai_core.buildin_tools.scheduler import query_scheduled_task

    sched_env["tasks"] = [_group_task()]
    ctx = _make_ctx(user_id="999", group_id="another_group")
    result = await query_scheduled_task(ctx, task_id="scheduled_task_5cad21ace9f5")
    assert "无权" in result
