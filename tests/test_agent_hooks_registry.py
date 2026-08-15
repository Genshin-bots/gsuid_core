"""Hook 总线契约：31 点位 / 执行序 / 能力票 / 超时 / fail-open / H29 门。

这些是套件化的地基。任何一条破了，后面 18 个套件的行为都不可信。
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from gsuid_core.ai_core.hooks import (
    HOOK_POINT_SPECS,
    HookDecision,
    AgentHookPoint,
    HookCapability,
    AgentHookResult,
    AgentHookContext,
    HookCapabilityError,
    spec_for,
    fire_hooks,
    hook_count,
    clear_hooks,
    on_agent_hook,
    hooks_registered,
)


@pytest.fixture(autouse=True)
def _clean_hooks():
    clear_hooks()
    yield
    clear_hooks()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_31_points_each_have_a_spec() -> None:
    """点位数与契约表一一对应：入站 1 + 外环 9 + 单次 run 20 + 建 session 1。"""
    assert len(AgentHookPoint) == 31, [p.name for p in AgentHookPoint]
    assert set(HOOK_POINT_SPECS) == set(AgentHookPoint)
    for point in AgentHookPoint:
        spec = spec_for(point)
        assert spec.anchor, f"{point.name} 缺内核锚点说明"
        assert spec.default_timeout_ms > 0
    # H05 是唯一允许的长超时（双路 + rerank）
    assert spec_for(AgentHookPoint.RETRIEVE_CONTEXT).default_timeout_ms == 15000
    longest = max(HOOK_POINT_SPECS.values(), key=lambda s: s.default_timeout_ms)
    assert longest.point is AgentHookPoint.RETRIEVE_CONTEXT
    # 出站热路径必须是 50ms 级
    for point in (
        AgentHookPoint.BEFORE_MODEL_REQUEST,
        AgentHookPoint.ON_TOOL_CALL,
        AgentHookPoint.BEFORE_TEXT_GATE,
        AgentHookPoint.AFTER_SEND,
    ):
        assert spec_for(point).default_timeout_ms <= 50, point.name


def test_point_ids_are_stable() -> None:
    """编号是稳定 ID：删点只标 deprecated，不复用编号。"""
    assert AgentHookPoint.ON_INBOUND.value == "H00"
    assert AgentHookPoint.RETRIEVE_CONTEXT.value == "H05"
    assert AgentHookPoint.COMPOSE_CONTEXT.value == "H06"
    assert AgentHookPoint.AFTER_BUILD_AGENT.value == "H15b"
    assert AgentHookPoint.ON_STABLE_CONTEXT.value == "H29"
    ids = [p.value for p in AgentHookPoint]
    assert len(set(ids)) == len(ids), "点位 ID 重复"


def test_priority_then_registration_order() -> None:
    """priority 越小越先；同优先级按注册序串行（环内有变异，不能并发）。"""
    order: List[str] = []

    @on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=200)
    async def second(ctx: AgentHookContext) -> None:
        order.append("second")

    @on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=100)
    async def first(ctx: AgentHookContext) -> None:
        order.append("first")

    @on_agent_hook(AgentHookPoint.COMPOSE_CONTEXT, priority=200)
    async def second_b(ctx: AgentHookContext) -> None:
        order.append("second_b")

    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    assert _run(fire_hooks(AgentHookPoint.COMPOSE_CONTEXT, ctx)) is HookDecision.CONTINUE
    assert order == ["first", "second", "second_b"], order


def test_sync_hook_runs_in_thread() -> None:
    seen: List[int] = []

    @on_agent_hook(AgentHookPoint.AFTER_SEND, priority=100)
    def sync_hook(ctx: AgentHookContext) -> None:
        seen.append(1)

    _run(fire_hooks(AgentHookPoint.AFTER_SEND, AgentHookContext(point=AgentHookPoint.AFTER_SEND)))
    assert seen == [1]


def test_exception_is_fail_open_and_not_escalated() -> None:
    """单个 hook 异常吞掉；**不得**升级成 ABORT_RUN，后续 hook 照跑。"""
    ran: List[str] = []

    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=100)
    async def boom(ctx: AgentHookContext) -> None:
        raise RuntimeError("kaboom")

    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=200)
    async def still_runs(ctx: AgentHookContext) -> None:
        ran.append("after")

    ctx = AgentHookContext(point=AgentHookPoint.AFTER_RUN)
    assert _run(fire_hooks(AgentHookPoint.AFTER_RUN, ctx)) is HookDecision.CONTINUE
    assert ran == ["after"]


def test_timeout_is_fail_open() -> None:
    @on_agent_hook(AgentHookPoint.AFTER_SEND, priority=100, timeout_ms=30)
    async def slow(ctx: AgentHookContext) -> None:
        await asyncio.sleep(2.0)
        raise AssertionError("should have been cancelled")

    ctx = AgentHookContext(point=AgentHookPoint.AFTER_SEND)
    assert _run(fire_hooks(AgentHookPoint.AFTER_SEND, ctx)) is HookDecision.CONTINUE


def test_empty_table_is_a_fast_path() -> None:
    assert not hooks_registered(AgentHookPoint.ON_THINKING)
    ctx = AgentHookContext(point=AgentHookPoint.ON_THINKING)
    assert _run(fire_hooks(AgentHookPoint.ON_THINKING, ctx)) is HookDecision.CONTINUE


def test_control_code_short_circuits() -> None:
    """首个非 CONTINUE 即短路，后续 hook 不跑。"""

    @on_agent_hook(AgentHookPoint.BEFORE_AI_CHAT, priority=100)
    async def abort_it(ctx: AgentHookContext) -> AgentHookResult:
        return ctx.abort("muted")

    @on_agent_hook(AgentHookPoint.BEFORE_AI_CHAT, priority=200)
    async def never(ctx: AgentHookContext) -> None:
        raise AssertionError("must not run after ABORT_RUN")

    ctx = AgentHookContext(point=AgentHookPoint.BEFORE_AI_CHAT)
    assert _run(fire_hooks(AgentHookPoint.BEFORE_AI_CHAT, ctx)) is HookDecision.ABORT_RUN
    assert ctx.decision_reason == "muted"


def test_unauthorized_capability_is_denied_not_silently_applied() -> None:
    """未授权的受控写操作抛 HookCapabilityError，且被 dispatcher fail-open 记 warning。"""
    ctx = AgentHookContext(point=AgentHookPoint.AFTER_SEND)
    with pytest.raises(HookCapabilityError):
        ctx.set_context_block("memory", "x")
    with pytest.raises(HookCapabilityError):
        ctx.set_intent("闲聊")
    with pytest.raises(HookCapabilityError):
        ctx.ensure_tools("web_search_tool")
    # H06 有 SET_BLOCK 但没有 SET_INTENT
    compose = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    compose.set_context_block("memory", "ok")
    assert compose.blocks["memory"] == "ok"
    with pytest.raises(HookCapabilityError):
        compose.set_intent("闲聊")
    assert HookCapability.SET_BLOCK in spec_for(AgentHookPoint.COMPOSE_CONTEXT).capabilities


def test_unknown_block_name_is_rejected() -> None:
    """未知块名一律拒绝，防套件私自插到身份锚前面。"""
    ctx = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    with pytest.raises(HookCapabilityError):
        ctx.set_context_block("my_private_block", "x")


def test_intent_values_are_constrained() -> None:
    ctx = AgentHookContext(point=AgentHookPoint.CLASSIFY)
    ctx.set_intent("问答")
    assert ctx.intent == "问答"
    with pytest.raises(HookCapabilityError):
        ctx.set_intent("胡说")


def test_h29_only_fires_while_building_a_session() -> None:
    """运行中禁止改 system：H29 在非建 session 阶段被硬拒（否则前缀缓存全废）。"""
    fired: List[int] = []

    @on_agent_hook(AgentHookPoint.ON_STABLE_CONTEXT, priority=100)
    async def stable(ctx: AgentHookContext) -> None:
        fired.append(1)
        ctx.set_context_block("self_model", "自述")

    ctx = AgentHookContext(point=AgentHookPoint.ON_STABLE_CONTEXT)
    _run(fire_hooks(AgentHookPoint.ON_STABLE_CONTEXT, ctx))
    assert not fired, "H29 在非建 session 阶段必须被拒"

    _run(fire_hooks(AgentHookPoint.ON_STABLE_CONTEXT, ctx, stable_context_phase=True))
    assert fired == [1]
    assert ctx.blocks["self_model"] == "自述"


def test_default_filters_skip_subagent_and_correction() -> None:
    """默认过滤器：子代理 / 纠正轮 / 框架轮不重入第三方 hook。"""
    ran: List[str] = []

    @on_agent_hook(AgentHookPoint.AFTER_CONTEXT, priority=100)
    async def normal(ctx: AgentHookContext) -> None:
        ran.append("normal")

    @on_agent_hook(AgentHookPoint.AFTER_CONTEXT, priority=110, include_subagent=True)
    async def also_sub(ctx: AgentHookContext) -> None:
        ran.append("sub_ok")

    sub_ctx = AgentHookContext(point=AgentHookPoint.AFTER_CONTEXT, is_subagent=True)
    _run(fire_hooks(AgentHookPoint.AFTER_CONTEXT, sub_ctx))
    assert ran == ["sub_ok"], ran

    ran.clear()
    corr = AgentHookContext(point=AgentHookPoint.AFTER_CONTEXT, is_correction=True)
    _run(fire_hooks(AgentHookPoint.AFTER_CONTEXT, corr))
    assert ran == [], "纠正轮默认不重入"

    ran.clear()
    plain = AgentHookContext(point=AgentHookPoint.AFTER_CONTEXT)
    _run(fire_hooks(AgentHookPoint.AFTER_CONTEXT, plain))
    assert ran == ["normal", "sub_ok"]


def test_create_by_filter() -> None:
    ran: List[str] = []

    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=100, create_by=("Chat",))
    async def chat_only(ctx: AgentHookContext) -> None:
        ran.append("chat")

    _run(fire_hooks(AgentHookPoint.AFTER_RUN, AgentHookContext(point=AgentHookPoint.AFTER_RUN, create_by="Agent")))
    assert ran == []
    _run(fire_hooks(AgentHookPoint.AFTER_RUN, AgentHookContext(point=AgentHookPoint.AFTER_RUN, create_by="Chat")))
    assert ran == ["chat"]


def test_hint_carries_owner_prefix_and_is_idempotent() -> None:
    """hint 必须带来源前缀（让 prepare_user 能与用户原话区分），且重复加不叠前缀。"""
    from gsuid_core.ai_core.hooks import KIT_HINT_PREFIX, PLUGIN_HINT_PREFIX, is_hook_hint

    ctx = AgentHookContext(point=AgentHookPoint.AFTER_CONTEXT)
    ctx.current_kit_id = "gscore.memory"
    ctx.append_user_hint("记得上次的事")
    assert ctx.hints[0].startswith(KIT_HINT_PREFIX)
    assert is_hook_hint(ctx.hints[0])

    ctx.current_kit_id = "MyPlugin"
    ctx.append_user_hint("本群自选：A、B")
    assert ctx.hints[1].startswith(PLUGIN_HINT_PREFIX)

    ctx.append_user_hint(ctx.hints[1])
    assert ctx.hints[2] == ctx.hints[1], "幂等：已带前缀不再叠"


def test_private_chat_group_id_is_always_none() -> None:
    """Context 的 group_id 私聊恒 None——这是记忆 scope 防回归的地基。"""
    from types import SimpleNamespace

    ctx = AgentHookContext(point=AgentHookPoint.RETRIEVE_CONTEXT)
    assert ctx.group_id is None
    ctx.ev = SimpleNamespace(user_id="u1", group_id=None, bot_id="B", session_id="s")  # type: ignore[assignment]
    assert ctx.group_id is None
    ctx.ev = SimpleNamespace(user_id="u1", group_id="g1", bot_id="B", session_id="s")  # type: ignore[assignment]
    assert ctx.group_id == "g1"


def test_drop_hooks_by_module_and_kit() -> None:
    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=100, kit_id="gscore.demo")
    async def kit_hook(ctx: AgentHookContext) -> None:
        pass

    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=110)
    async def module_hook(ctx: AgentHookContext) -> None:
        pass

    from gsuid_core.ai_core.hooks import drop_hooks_for_kit, drop_hooks_for_module

    assert hook_count() == 2
    assert drop_hooks_for_kit("gscore.demo") == 1
    assert hook_count() == 1
    # 插件热重载按 __module__ 前缀摘钩；本测试模块名即前缀
    assert drop_hooks_for_module(module_hook.__module__) == 1
    assert hook_count() == 0
