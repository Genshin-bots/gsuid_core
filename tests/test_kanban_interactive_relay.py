"""交互式 create_subagent 的"执行体静默"登记语义回归。

生产 ``create_subagent`` 派完即走，结论只走 deferred 回灌主 session
（``_wake_main_agent_for_delivery``），**不再**走 Kanban_Relay 推群。
``_INTERACTIVE_RELAY_ROOTS`` 消费一次：避免执行体再自动播报导致双份。

这里锁死"消费一次"的核心不变量——它是整个无竞态设计的地基。
"""

import pytest

# kanban_executor 的导入链会拉起 skills / web_search 等重依赖；缺可选依赖的精简环境跳过整个文件
# （生产 / CI 有这些依赖时照常运行）。
_ke = pytest.importorskip("gsuid_core.ai_core.planning.kanban_executor")

mark_interactive_relay_root = _ke.mark_interactive_relay_root
discard_interactive_relay_root = _ke.discard_interactive_relay_root
_consume_interactive_relay = _ke._consume_interactive_relay


def test_marked_root_is_consumed_exactly_once() -> None:
    """登记后第一次消费返回 True 并移除；第二次消费返回 False（不会二次静默）。"""
    rid = "root_consume_once_001"
    mark_interactive_relay_root(rid)
    assert _consume_interactive_relay(rid) is True, "首次消费应命中静默"
    assert _consume_interactive_relay(rid) is False, "消费应读即弃，第二次不得再命中"


def test_unmarked_root_never_suppresses() -> None:
    """没登记过的 root（如后台 kanban 定时 tick）永远返回 False → 照常推群。"""
    assert _consume_interactive_relay("root_never_marked_xyz") is False


def test_discard_before_consume_clears_interactive() -> None:
    """discard 后 executor 消费不到 interactive 标记（不回退 Relay 推群）。"""
    rid = "root_timeout_discard_002"
    mark_interactive_relay_root(rid)
    discard_interactive_relay_root(rid)
    assert _consume_interactive_relay(rid) is False, "已 discard 的 root 不应再静默"


def test_discard_is_idempotent_and_safe_on_unknown() -> None:
    """discard 不存在的 root 不抛异常（幂等）。"""
    discard_interactive_relay_root("root_not_present_zzz")  # 不应抛
    assert _consume_interactive_relay("root_not_present_zzz") is False


def test_delivery_frame_includes_owner() -> None:
    """回灌帧必须钉死发起人，否则群会话里会把 A 的图发给 B。"""
    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.ai_core.planning.kanban_executor import _format_delivery_for_main_agent

    task = AIAgentTask(ordinal=7, display_name="图", owner_user_id="100000002", goal="x")
    text = _format_delivery_for_main_agent(task, "", [])
    assert "@100000002" in text
    assert "禁止改 user_id" in text


def test_same_owner_distinct_roots_each_reflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一发起人的两条任务各自回灌；同一条根的第二次才禁止再委派。"""
    import asyncio
    from unittest.mock import AsyncMock

    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.ai_core.planning.runtime import current_delivery_root_id

    sid = "sess-roots-reflow"
    owner = "owner-roots-reflow"
    root_a = "root-a-reflow"
    root_b = "root-b-reflow"
    seen_roots: list[str] = []
    seen_blocked: list[bool] = []

    class _WakeSession:
        async def run(
            self,
            user_message: str = "",
            bot: object = None,
            ev: object = None,
            return_mode: str = "",
            has_active_task: bool = False,
            is_framework_injection: bool = False,
        ) -> str:
            seen_roots.append(current_delivery_root_id())
            seen_blocked.append(_ke.delivery_wake_blocks_new_delegate(sid))
            return ""

    sess = _WakeSession()

    class _Reg:
        def get_ai_session(self, session_id: str) -> _WakeSession:
            return sess

    def _make_task(root_id: str, name: str) -> AIAgentTask:
        return AIAgentTask(
            id=root_id,
            root_task_id=root_id,
            session_id=sid,
            owner_user_id=owner,
            goal=name,
            bot_id="bot",
            bot_self_id="self",
            user_type="group",
            group_id="g-roots-reflow",
            display_name=name,
        )

    monkeypatch.setattr(_ke.AIAgentArtifact, "list_for_task", AsyncMock(return_value=[]))
    monkeypatch.setattr(_ke, "_get_bot", lambda _task, _ev: object())
    monkeypatch.setattr(
        "gsuid_core.ai_core.session_registry.get_ai_session_registry",
        lambda: _Reg(),
    )

    async def _go() -> None:
        await _ke._wake_main_agent_for_delivery_now(_make_task(root_a, "甲"), "done-a")
        await _ke._wake_main_agent_for_delivery_now(_make_task(root_b, "乙"), "done-b")
        await _ke._wake_main_agent_for_delivery_now(_make_task(root_a, "甲"), "done-a2")

    keys = (f"{sid}|{root_a}", f"{sid}|{root_b}", f"{sid}|{owner}")
    try:
        asyncio.run(_go())
        assert seen_roots == [root_a, root_b, root_a]
        assert seen_blocked == [False, False, True]
        assert _ke.should_block_nested_delegate(sid, owner) is False
        assert _ke.should_block_nested_delegate(sid, root_b) is False
        assert _ke.should_block_nested_delegate(sid, root_a) is True
    finally:
        for key in keys:
            _ke._delivery_wake_hits.pop(key, None)


def test_fallback_notify_does_not_spend_delivery_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有主会话时的兜底通知不算回灌，下一次真正唤醒仍可委派。"""
    import time
    import asyncio
    from unittest.mock import AsyncMock

    from gsuid_core.ai_core.planning.models import AIAgentTask

    sid = "sess-wake-no-bot-40096783"
    owner = "owner-wake-no-bot"
    root = "root-wake-no-bot"
    task = AIAgentTask(
        id=root,
        root_task_id=root,
        session_id=sid,
        owner_user_id=owner,
        goal="deliver",
        bot_id="bot",
        bot_self_id="self",
        user_type="group",
        group_id="g-wake-no-bot",
        display_name="图",
    )
    monkeypatch.setattr(_ke.AIAgentArtifact, "list_for_task", AsyncMock(return_value=[]))
    monkeypatch.setattr(_ke, "_get_bot", lambda _task, _ev: None)

    async def _go() -> int:
        await _ke._wake_main_agent_for_delivery_now(task, "done")
        return _ke.record_delivery_wake(sid, root, now=time.time())

    key = f"{sid}|{root}"
    owner_key = f"{sid}|{owner}"
    try:
        counted = asyncio.run(_go())
        assert owner_key not in _ke._delivery_wake_hits
        assert counted == 1
        assert _ke.should_block_nested_delegate(sid, root, now=time.time()) is False
    finally:
        _ke._delivery_wake_hits.pop(key, None)
        _ke._delivery_wake_hits.pop(owner_key, None)
