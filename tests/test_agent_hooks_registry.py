"""Hook 总线契约：31 点位 / 执行序 / 能力票 / 超时 / fail-open。"""

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
    drop_hooks_for_kit,
    drop_hooks_for_module,
)


@pytest.fixture(autouse=True)
def _clean_hooks():
    clear_hooks()
    yield
    clear_hooks()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_hook_points_specs_and_ids() -> None:
    assert len(AgentHookPoint) == 31, [p.name for p in AgentHookPoint]
    assert set(HOOK_POINT_SPECS) == set(AgentHookPoint)
    for point in AgentHookPoint:
        spec = spec_for(point)
        assert spec.anchor and spec.default_timeout_ms > 0
    assert spec_for(AgentHookPoint.RETRIEVE_CONTEXT).default_timeout_ms == 15000
    longest = max(HOOK_POINT_SPECS.values(), key=lambda s: s.default_timeout_ms)
    assert longest.point is AgentHookPoint.RETRIEVE_CONTEXT
    for point in (
        AgentHookPoint.BEFORE_MODEL_REQUEST,
        AgentHookPoint.ON_TOOL_CALL,
        AgentHookPoint.BEFORE_TEXT_GATE,
        AgentHookPoint.AFTER_SEND,
    ):
        assert spec_for(point).default_timeout_ms <= 50, point.name

    assert AgentHookPoint.ON_INBOUND.value == "H00"
    assert AgentHookPoint.RETRIEVE_CONTEXT.value == "H05"
    assert AgentHookPoint.COMPOSE_CONTEXT.value == "H06"
    assert AgentHookPoint.AFTER_BUILD_AGENT.value == "H15b"
    assert AgentHookPoint.ON_STABLE_CONTEXT.value == "H29"
    ids = [p.value for p in AgentHookPoint]
    assert len(set(ids)) == len(ids)


def test_dispatch_order_sync_fail_open_timeout_and_short_circuit() -> None:
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
    assert order == ["first", "second", "second_b"]

    seen: List[int] = []

    @on_agent_hook(AgentHookPoint.AFTER_SEND, priority=100)
    def sync_hook(ctx: AgentHookContext) -> None:
        seen.append(1)

    @on_agent_hook(AgentHookPoint.AFTER_SEND, priority=200, timeout_ms=30)
    async def slow(ctx: AgentHookContext) -> None:
        await asyncio.sleep(2.0)
        raise AssertionError("should have been cancelled")

    assert _run(fire_hooks(AgentHookPoint.AFTER_SEND, AgentHookContext(point=AgentHookPoint.AFTER_SEND))) is (
        HookDecision.CONTINUE
    )
    assert seen == [1]

    ran: List[str] = []

    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=100)
    async def boom(ctx: AgentHookContext) -> None:
        raise RuntimeError("kaboom")

    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=200)
    async def still_runs(ctx: AgentHookContext) -> None:
        ran.append("after")

    assert _run(fire_hooks(AgentHookPoint.AFTER_RUN, AgentHookContext(point=AgentHookPoint.AFTER_RUN))) is (
        HookDecision.CONTINUE
    )
    assert ran == ["after"]

    assert not hooks_registered(AgentHookPoint.ON_THINKING)
    assert _run(fire_hooks(AgentHookPoint.ON_THINKING, AgentHookContext(point=AgentHookPoint.ON_THINKING))) is (
        HookDecision.CONTINUE
    )

    @on_agent_hook(AgentHookPoint.BEFORE_AI_CHAT, priority=100)
    async def abort_it(ctx: AgentHookContext) -> AgentHookResult:
        return ctx.abort("muted")

    @on_agent_hook(AgentHookPoint.BEFORE_AI_CHAT, priority=200)
    async def never(ctx: AgentHookContext) -> None:
        raise AssertionError("must not run after ABORT_RUN")

    abort_ctx = AgentHookContext(point=AgentHookPoint.BEFORE_AI_CHAT)
    assert _run(fire_hooks(AgentHookPoint.BEFORE_AI_CHAT, abort_ctx)) is HookDecision.ABORT_RUN
    assert abort_ctx.decision_reason == "muted"


def test_capabilities_blocks_and_drop_hooks() -> None:
    ctx = AgentHookContext(point=AgentHookPoint.AFTER_SEND)
    with pytest.raises(HookCapabilityError):
        ctx.set_context_block("memory", "x")
    with pytest.raises(HookCapabilityError):
        ctx.set_intent("闲聊")
    with pytest.raises(HookCapabilityError):
        ctx.ensure_tools("web_search_tool")

    compose = AgentHookContext(point=AgentHookPoint.COMPOSE_CONTEXT)
    compose.set_context_block("memory", "ok")
    assert compose.blocks["memory"] == "ok"
    with pytest.raises(HookCapabilityError):
        compose.set_intent("闲聊")
    with pytest.raises(HookCapabilityError):
        compose.set_context_block("my_private_block", "x")
    assert HookCapability.SET_BLOCK in spec_for(AgentHookPoint.COMPOSE_CONTEXT).capabilities

    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=100, kit_id="gscore.demo")
    async def kit_hook(ctx: AgentHookContext) -> None:
        pass

    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=110)
    async def module_hook(ctx: AgentHookContext) -> None:
        pass

    assert hook_count() == 2
    assert drop_hooks_for_kit("gscore.demo") == 1
    assert hook_count() == 1
    assert drop_hooks_for_module(module_hook.__module__) == 1
    assert hook_count() == 0
