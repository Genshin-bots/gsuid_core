"""历史高低水位裁剪 + report 占位 + 定时任务溯源回归测试（plans/prod_session_review §5/§18/§25）。

2026-07-16 生产观察：
- auto_compact 55/58 轮触发（16~21 → 15），历史头部每轮变化 → provider 前缀缓存
  在消息段永不命中（全天命中率卡 54%）；
- 早柚自己 4.5h 前创建的盯盘任务，被问"这是谁要的提醒"时完全无法追溯——
  list_scheduled_tasks 只按提问者 user_id 过滤，别人建的任务根本查不到。

对应修复：
- ``extract_history``：超过 max_history 才裁、一次裁到低水位（0.6x），裁剪间隔内前缀稳定；
- ``_compact_report_blocks_in_history``：持久历史中 <report> 正文换占位符（省 token + 切断漂移固化）；
- ``list_scheduled_tasks`` 只列提问者自己的任务；``query_scheduled_task`` 同群成员可凭 ID 只读。
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

from gsuid_core.ai_core.utils import (
    compact_session_history,
    _compact_report_blocks_in_history,
)

# 与 gs_agent._HISTORY_TRIM_RATIO 保持一致（单测不 import 重依赖 gs_agent）
_HISTORY_TRIM_RATIO = 0.6

# ─────────────────────────────────────────────
# compact_session_history / 保头裁中段
# ─────────────────────────────────────────────


def _turn(i: int) -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content=f"[用户发言]\n消息{i}")]),
        ModelResponse(parts=[TextPart(content=f"回复{i}")]),
    ]


def test_no_trim_below_watermark() -> None:
    """未超 max_history 时一条都不动——头部稳定是缓存命中的前提。"""
    history = []
    for i in range(7):
        history.extend(_turn(i))  # 14 条
    out, did = compact_session_history(list(history), max_history=15, trim_ratio=_HISTORY_TRIM_RATIO)
    assert did is False
    assert out == history


def test_trim_goes_to_low_watermark_not_max() -> None:
    """超过 max_history 时一次裁到低水位；保头裁中段，尾部仍是最新消息。"""
    history = []
    for i in range(9):
        history.extend(_turn(i))  # 18 条 > 15
    out, did = compact_session_history(list(history), max_history=15, trim_ratio=_HISTORY_TRIM_RATIO)
    low = int(15 * _HISTORY_TRIM_RATIO)
    assert did is True
    assert len(out) <= low + 2  # 工具配对可能略超
    assert len(out) < 18
    # 头部仍是会话最早消息（前缀缓存红线）
    first = out[0]
    assert isinstance(first, ModelRequest)
    assert isinstance(first.parts[0], UserPromptPart)
    assert first.parts[0].content == "[用户发言]\n消息0"
    # 尾部仍是最新
    last = out[-1]
    assert isinstance(last, ModelResponse)
    first_part = last.parts[0]
    assert isinstance(first_part, TextPart) and first_part.content == "回复8"


def test_trim_keeps_original_prefix_forever() -> None:
    """多次 compact 后 history[0] 仍是原始首条——绝不砍头/插锚点。"""
    max_history = 20
    low = int(max_history * _HISTORY_TRIM_RATIO)
    history: list = []
    for i in range(20):
        history.extend(_turn(i))
    original_head = history[0]
    out, _ = compact_session_history(list(history), max_history=max_history, trim_ratio=_HISTORY_TRIM_RATIO)
    assert out[0] is original_head
    assert len(out) <= low + 2

    # 再撑爆水位 compact 一次，头部对象仍不变
    grown = list(out)
    for j in range(15):
        grown.extend(_turn(200 + j))
    out2, _ = compact_session_history(grown, max_history=max_history, trim_ratio=_HISTORY_TRIM_RATIO)
    assert out2[0] is original_head


def test_trim_interval_gives_stable_prefix() -> None:
    """裁剪后继续追加若干轮都不再触发裁剪——这段窗口内历史头部字节稳定。"""
    max_history = 20
    history = []
    # 严格超过 max_history（=20 时 10 轮刚好 20 条不触发，需 11 轮）
    for i in range(11):
        history.extend(_turn(i))
    out, did = compact_session_history(list(history), max_history=max_history, trim_ratio=_HISTORY_TRIM_RATIO)
    assert did is True
    assert len(out) < len(history)
    stable_head = list(out)

    # headroom 内追加不再触发裁剪，头部对象序列不变
    headroom_pairs = max(1, (max_history - len(out)) // 2)
    cur = list(out)
    for j in range(headroom_pairs):
        cur.extend(_turn(100 + j))
        cur, did2 = compact_session_history(cur, max_history=max_history, trim_ratio=_HISTORY_TRIM_RATIO)
        assert did2 is False
    assert cur[: len(stable_head)] == stable_head


def test_zero_max_history_clears() -> None:
    out, did = compact_session_history(_turn(1), max_history=0, trim_ratio=_HISTORY_TRIM_RATIO)
    assert out == []
    assert did is True


# ─────────────────────────────────────────────
# _compact_report_blocks_in_history
# ─────────────────────────────────────────────


def test_report_body_stripped_title_in_metadata() -> None:
    """历史只留台词；标题进 sent_reports metadata（非占位正文）。"""
    md = "| 指标 | 数值 |\n|---|---|\n| 营收 | +12% |"
    msg = ModelResponse(parts=[TextPart(content=f'唔…看这张…\n<report title="XX速览">{md}</report>')])
    replaced = _compact_report_blocks_in_history([msg])
    assert replaced == 1
    part = msg.parts[0]
    assert isinstance(part, TextPart)
    assert "营收" not in part.content
    assert "唔…看这张…" in part.content
    assert msg.metadata is not None
    assert "XX速览" in (msg.metadata.get("sent_reports") or [])


def test_untitled_report_gets_generic_title_in_metadata() -> None:
    msg = ModelResponse(parts=[TextPart(content="<report>长内容</report>")])
    _compact_report_blocks_in_history([msg])
    part = msg.parts[0]
    assert isinstance(part, TextPart)
    assert "长内容" not in part.content
    assert msg.metadata is not None
    assert "分析资料" in (msg.metadata.get("sent_reports") or [])


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
        "user_id": "100000002",  # 化名：小北
        "group_id": "200000001",
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
        rows = env["tasks"]
        if "user_id" in kwargs:
            uid = kwargs["user_id"]
            return [t for t in rows if t.user_id == uid]
        if "task_id" in kwargs:
            tid = kwargs["task_id"]
            return [t for t in rows if t.task_id == tid]
        if "group_id" in kwargs:
            gid = kwargs["group_id"]
            return [t for t in rows if t.group_id == gid]
        return rows

    monkeypatch.setattr(sched_mod.AIScheduledTask, "select_rows", fake_select_rows)
    return env


@pytest.mark.anyio
async def test_group_list_does_not_include_others_tasks(sched_env: dict) -> None:
    """列表不外泄他人任务；凭 ID 的只读走 query_scheduled_task。"""
    from gsuid_core.ai_core.buildin_tools.scheduler import list_scheduled_tasks

    sched_env["tasks"] = [_group_task()]
    ctx = _make_ctx(user_id="100000003", group_id="200000001")
    result = await list_scheduled_tasks(ctx)

    assert {"user_id": "100000003"} in sched_env["select_kwargs"]
    assert not any("group_id" in kw for kw in sched_env["select_kwargs"])
    assert "scheduled_task_5cad21ace9f5" not in result


@pytest.mark.anyio
async def test_group_list_includes_own_task(sched_env: dict) -> None:
    from gsuid_core.ai_core.buildin_tools.scheduler import list_scheduled_tasks

    sched_env["tasks"] = [_group_task(user_id="100000003")]
    ctx = _make_ctx(user_id="100000003", group_id="200000001")
    result = await list_scheduled_tasks(ctx)
    assert "scheduled_task_5cad21ace9f5" in result


@pytest.mark.anyio
async def test_private_chat_still_filters_by_user(sched_env: dict) -> None:
    from gsuid_core.ai_core.buildin_tools.scheduler import list_scheduled_tasks

    sched_env["tasks"] = []
    ctx = _make_ctx(user_id="100000003", group_id=None)
    await list_scheduled_tasks(ctx)
    assert sched_env["select_kwargs"] == [{"user_id": "100000003"}]


@pytest.mark.anyio
async def test_query_task_readable_by_same_group_member(sched_env: dict) -> None:
    """同群成员可按尾注里的任务 ID 查详情（只读）。"""
    from gsuid_core.ai_core.buildin_tools.scheduler import query_scheduled_task

    sched_env["tasks"] = [_group_task()]
    ctx = _make_ctx(user_id="100000003", group_id="200000001")
    result = await query_scheduled_task(ctx, task_id="scheduled_task_5cad21ace9f5")
    assert "无权" not in result
    assert "100000002" in result


@pytest.mark.anyio
async def test_query_task_denied_for_outsider(sched_env: dict) -> None:
    """非发起人且不在同一群：仍然无权查看。"""
    from gsuid_core.ai_core.buildin_tools.scheduler import query_scheduled_task

    sched_env["tasks"] = [_group_task()]
    ctx = _make_ctx(user_id="999", group_id="another_group")
    result = await query_scheduled_task(ctx, task_id="scheduled_task_5cad21ace9f5")
    assert "无权" in result
