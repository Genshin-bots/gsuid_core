"""gsuid_core 新增 ai_tools 单测：list/pause/resume_my_kanban。

覆盖：
- list_my_kanban_tasks: 列出当前 owner 名下的根任务（active/all/failed/completed）
- pause_my_kanban_tree: 周期模板走 disarm_template，一次性走 status=paused
- resume_my_kanban_tree: 周期模板走 arm_recurring_subtask，一次性走 kick_root

测试用 asyncio.run 包装（不依赖 pytest-asyncio）。
"""

import asyncio
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch


# ============================================================
# Helpers
# ============================================================
def _make_task(
    ordinal: int = 1,
    goal: str = "测试任务",
    status: str = "running",
    recurring_trigger: Optional[str] = None,
    display_name: str = "Test",
    failure_reason: str = "",
    node_kind: str = "root",
    root_task_id: str = "rt_001",
):
    t = MagicMock()
    t.id = f"task_{ordinal:03d}"
    t.ordinal = ordinal
    t.goal = goal
    t.status = status
    t.recurring_trigger = recurring_trigger
    t.display_name = display_name
    t.failure_reason = failure_reason
    t.node_kind = node_kind
    t.root_task_id = root_task_id
    return t


def _make_ctx(ev: Optional[Any] = None):
    """构造一个 RunContext[ToolContext]"""
    from gsuid_core.ai_core.models import ToolContext

    ctx = MagicMock()
    ctx.deps = ToolContext(
        bot=None,
        ev=ev,
        parent_session_id="test_session",
    )
    return ctx


def _run(coro):
    return asyncio.run(coro)


# ============================================================
# list_my_kanban_tasks
# ============================================================
def test_list_no_ev():
    from gsuid_core.ai_core.planning.kanban_tools import list_my_kanban_tasks

    ctx = _make_ctx(ev=None)
    ctx.deps.ev = None
    result = _run(list_my_kanban_tasks(ctx, goal_filter="", status="active"))
    assert "无法获取会话信息" in result
    print("[OK] list 无 ev → 警告")


def test_list_no_tasks():
    from gsuid_core.ai_core.planning.kanban_tools import list_my_kanban_tasks

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)

    with patch("gsuid_core.ai_core.planning.models.AIAgentTask") as mock_task:
        mock_task.list_for_owner = AsyncMock(return_value=[])
        result = _run(list_my_kanban_tasks(ctx, "", "active"))
        assert "没有 Kanban 任务树" in result
    print("[OK] list 无任务 → 提示")


def test_list_active_running():
    from gsuid_core.ai_core.planning.kanban_tools import list_my_kanban_tasks

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    tasks = [
        _make_task(1, "AI 模拟盘 init", status="running"),
        _make_task(2, "AI 模拟盘 周期", status="running"),
    ]

    with patch("gsuid_core.ai_core.planning.models.AIAgentTask") as mock_task:
        mock_task.list_for_owner = AsyncMock(return_value=tasks)
        result = _run(list_my_kanban_tasks(ctx, "", "active"))
        assert "#1" in result
        assert "#2" in result
        assert "AI 模拟盘" in result
        mock_task.list_for_owner.assert_called_once_with(
            "u1",
            only_active=True,
            root_only=True,
        )
    print("[OK] list active 列出 2 个 running 任务")


def test_list_goal_filter():
    from gsuid_core.ai_core.planning.kanban_tools import list_my_kanban_tasks

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    tasks = [
        _make_task(1, "AI 模拟盘 init", status="running"),
        _make_task(2, "新闻推送", status="running"),
    ]

    with patch("gsuid_core.ai_core.planning.models.AIAgentTask") as mock_task:
        mock_task.list_for_owner = AsyncMock(return_value=tasks)
        result = _run(list_my_kanban_tasks(ctx, "AI 模拟盘", "active"))
        assert "#1" in result
        assert "#2" not in result
        assert "新闻推送" not in result
    print("[OK] list goal_filter 过滤")


def test_list_status_filter_failed():
    from gsuid_core.ai_core.planning.kanban_tools import list_my_kanban_tasks

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    tasks = [
        _make_task(1, "已失败", status="failed", failure_reason="出错了"),
        _make_task(2, "已完成", status="completed"),
    ]

    with patch("gsuid_core.ai_core.planning.models.AIAgentTask") as mock_task:
        mock_task.list_for_owner = AsyncMock(return_value=tasks)
        result = _run(list_my_kanban_tasks(ctx, "", "failed"))
        assert "#1" in result
        assert "#2" not in result
        assert "出错了" in result
    print("[OK] list status=failed 过滤")


def test_list_table_truncation():
    from gsuid_core.ai_core.planning.kanban_tools import list_my_kanban_tasks

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    tasks = [_make_task(i, f"任务{i}", status="running") for i in range(1, 50)]

    with patch("gsuid_core.ai_core.planning.models.AIAgentTask") as mock_task:
        mock_task.list_for_owner = AsyncMock(return_value=tasks)
        result = _run(list_my_kanban_tasks(ctx, "", "active"))
        assert "还有 19 棵未列出" in result
    print("[OK] list 表格截断到 30")


def test_list_empty_after_filter():
    from gsuid_core.ai_core.planning.kanban_tools import list_my_kanban_tasks

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    tasks = [_make_task(1, "新闻推送", status="running")]

    with patch("gsuid_core.ai_core.planning.models.AIAgentTask") as mock_task:
        mock_task.list_for_owner = AsyncMock(return_value=tasks)
        result = _run(list_my_kanban_tasks(ctx, "AI 模拟盘", "active"))
        assert "过滤后无任务" in result
    print("[OK] list 过滤后空 → 提示")


# ============================================================
# pause_my_kanban_tree
# ============================================================
def test_pause_no_ev():
    from gsuid_core.ai_core.planning.kanban_tools import pause_my_kanban_tree

    ctx = _make_ctx(ev=None)
    ctx.deps.ev = None
    result = _run(pause_my_kanban_tree(ctx, "any", ""))
    assert "无法获取会话信息" in result
    print("[OK] pause 无 ev → 警告")


def test_pause_no_task_found():
    from gsuid_core.ai_core.planning.kanban_tools import pause_my_kanban_tree

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)

    with patch(
        "gsuid_core.ai_core.planning.kanban_tools._resolve_subtask",
        AsyncMock(return_value=None),
    ):
        result = _run(pause_my_kanban_tree(ctx, "nonexistent", ""))
        assert "找不到任务" in result
    print("[OK] pause 找不到任务 → 提示")


def test_pause_periodic_uses_disarm():
    from gsuid_core.ai_core.planning.kanban_tools import pause_my_kanban_tree

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    root = _make_task(
        ordinal=1,
        goal="AI 模拟盘",
        recurring_trigger="cron:0,30 9-14 * * 1-5",
    )
    sub = _make_task(ordinal=1, node_kind="subtask", root_task_id="rt_1")

    with (
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_subtask",
            AsyncMock(return_value=sub),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_root_from_subtask",
            AsyncMock(return_value=root),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban",
        ) as mock_kanban_mod,
    ):
        mock_kanban_mod.disarm_template = AsyncMock(return_value=True)
        result = _run(pause_my_kanban_tree(ctx, "AI 模拟盘", "测试暂停"))
        assert "已暂停周期" in result
        mock_kanban_mod.disarm_template.assert_called_once_with("task_001")
    print("[OK] pause 周期模板 → disarm_template")


def test_pause_one_shot_uses_status_pause():
    from gsuid_core.ai_core.planning.kanban_tools import pause_my_kanban_tree

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    root = _make_task(ordinal=1, goal="一次性", recurring_trigger=None)
    sub = _make_task(ordinal=1, node_kind="subtask", root_task_id="rt_1")

    with (
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_subtask",
            AsyncMock(return_value=sub),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_root_from_subtask",
            AsyncMock(return_value=root),
        ),
        patch(
            "gsuid_core.ai_core.planning.models.AIAgentTask",
        ) as mock_task,
    ):
        mock_task.update_data_by_data = AsyncMock()
        result = _run(pause_my_kanban_tree(ctx, "一次性", ""))
        assert "已暂停一次性" in result
        mock_task.update_data_by_data.assert_called_once()
    print("[OK] pause 一次性 → status=paused")


def test_pause_periodic_disarm_fails():
    from gsuid_core.ai_core.planning.kanban_tools import pause_my_kanban_tree

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    root = _make_task(ordinal=1, goal="AI 模拟盘", recurring_trigger="cron:...")
    sub = _make_task(ordinal=1, node_kind="subtask", root_task_id="rt_1")

    with (
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_subtask",
            AsyncMock(return_value=sub),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_root_from_subtask",
            AsyncMock(return_value=root),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban",
        ) as mock_kanban_mod,
    ):
        mock_kanban_mod.disarm_template = AsyncMock(return_value=False)
        result = _run(pause_my_kanban_tree(ctx, "AI 模拟盘", ""))
        assert "disarm 失败" in result
    print("[OK] pause 周期模板 disarm 失败 → 错误提示")


# ============================================================
# resume_my_kanban_tree
# ============================================================
def test_resume_no_ev():
    from gsuid_core.ai_core.planning.kanban_tools import resume_my_kanban_tree

    ctx = _make_ctx(ev=None)
    ctx.deps.ev = None
    result = _run(resume_my_kanban_tree(ctx, "any"))
    assert "无法获取会话信息" in result
    print("[OK] resume 无 ev → 警告")


def test_resume_periodic_uses_arm():
    from gsuid_core.ai_core.planning.kanban_tools import resume_my_kanban_tree

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    root = _make_task(ordinal=1, goal="AI 模拟盘", recurring_trigger="cron:0,30 9-14 * * 1-5")
    sub = _make_task(ordinal=1, node_kind="subtask", root_task_id="rt_1")

    with (
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_subtask",
            AsyncMock(return_value=sub),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_root_from_subtask",
            AsyncMock(return_value=root),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban",
        ) as mock_kanban_mod,
    ):
        mock_kanban_mod.arm_recurring_subtask = AsyncMock(return_value=(True, "ok"))
        result = _run(resume_my_kanban_tree(ctx, "AI 模拟盘"))
        assert "已恢复周期" in result
        mock_kanban_mod.arm_recurring_subtask.assert_called_once()
    print("[OK] resume 周期模板 → arm_recurring_subtask")


def test_resume_one_shot_kicks_root():
    from gsuid_core.ai_core.planning.kanban_tools import resume_my_kanban_tree

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    root = _make_task(ordinal=1, goal="一次性", recurring_trigger=None)
    sub = _make_task(ordinal=1, node_kind="subtask", root_task_id="rt_1")

    created_tasks = []

    def fake_create_task(coro):
        created_tasks.append(coro)
        # 关闭 coroutine 避免 warning
        try:
            coro.close()
        except Exception:
            pass
        return MagicMock()

    with (
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_subtask",
            AsyncMock(return_value=sub),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_root_from_subtask",
            AsyncMock(return_value=root),
        ),
        patch(
            "gsuid_core.ai_core.planning.models.AIAgentTask",
        ) as mock_task,
    ):
        mock_task.update_data_by_data = AsyncMock()
        # 直接 patch asyncio.create_task（全局 asyncio 模块）
        with patch("asyncio.create_task", side_effect=fake_create_task):
            result = _run(resume_my_kanban_tree(ctx, "一次性"))
            assert "已重新派发一次性" in result
            mock_task.update_data_by_data.assert_called_once()
            assert len(created_tasks) == 1
    print("[OK] resume 一次性 → status=pending + kick_root")


def test_resume_periodic_arm_fails():
    from gsuid_core.ai_core.planning.kanban_tools import resume_my_kanban_tree

    ev = MagicMock(user_id="u1", group_id="g1")
    ctx = _make_ctx(ev=ev)
    root = _make_task(ordinal=1, goal="AI 模拟盘", recurring_trigger="cron:...")
    sub = _make_task(ordinal=1, node_kind="subtask", root_task_id="rt_1")

    with (
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_subtask",
            AsyncMock(return_value=sub),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban_tools._resolve_root_from_subtask",
            AsyncMock(return_value=root),
        ),
        patch(
            "gsuid_core.ai_core.planning.kanban",
        ) as mock_kanban_mod,
    ):
        mock_kanban_mod.arm_recurring_subtask = AsyncMock(return_value=(False, "冲突"))
        result = _run(resume_my_kanban_tree(ctx, "AI 模拟盘"))
        assert "arm 失败" in result
        assert "冲突" in result
    print("[OK] resume 周期模板 arm 失败 → 错误提示")


if __name__ == "__main__":
    test_list_no_ev()
    test_list_no_tasks()
    test_list_active_running()
    test_list_goal_filter()
    test_list_status_filter_failed()
    test_list_table_truncation()
    test_list_empty_after_filter()
    test_pause_no_ev()
    test_pause_no_task_found()
    test_pause_periodic_uses_disarm()
    test_pause_one_shot_uses_status_pause()
    test_pause_periodic_disarm_fails()
    test_resume_no_ev()
    test_resume_periodic_uses_arm()
    test_resume_one_shot_kicks_root()
    test_resume_periodic_arm_fails()
    print("\n[SUCCESS] my_kanban_tools 全部 16 个测试通过！")
