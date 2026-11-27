"""Hook 总线契约：已接线点位 / 执行序 / 能力票 / 超时 / fail-open。"""

from __future__ import annotations

import re
import asyncio
from collections.abc import Iterator

import pytest

from gsuid_core.ai_core.hooks import (
    HOOK_POINT_SPECS,
    HookDecision,
    AgentHookPoint,
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
from gsuid_core.ai_core.configs.ai_config import ai_config


@pytest.fixture(autouse=True)
def _clean_hooks(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(ai_config.get_config("enable"), "data", True)
    monkeypatch.setattr(ai_config.get_config("agent_hooks_enable"), "data", True)
    clear_hooks()
    yield
    clear_hooks()


def test_hook_points_specs_and_ids() -> None:
    assert set(HOOK_POINT_SPECS) == set(AgentHookPoint)
    seen_ids: set[str] = set()
    for point in AgentHookPoint:
        spec = spec_for(point)
        assert spec.anchor and spec.default_timeout_ms > 0
        assert re.fullmatch(r"H\d{2}", point.value) is not None, point
        assert point.value not in seen_ids, point.value
        seen_ids.add(point.value)

    # 已发布点位编号属于插件/套件稳定契约（points.py），禁止变更或复用
    published_ids = {
        AgentHookPoint.ON_INBOUND: "H00",
        AgentHookPoint.BEFORE_AI_CHAT: "H01",
        AgentHookPoint.AFTER_SESSION: "H02",
        AgentHookPoint.CLASSIFY: "H03",
        AgentHookPoint.RETRIEVE_CONTEXT: "H05",
        AgentHookPoint.COMPOSE_CONTEXT: "H06",
        AgentHookPoint.AFTER_CONTEXT: "H07",
        AgentHookPoint.AFTER_RUN: "H08",
        AgentHookPoint.ASSEMBLE_TOOLS: "H14",
        AgentHookPoint.AFTER_ASSEMBLE_TOOLS: "H15",
        AgentHookPoint.ON_STABLE_CONTEXT: "H29",
    }
    for point, expected_id in published_ids.items():
        assert point.value == expected_id, point

    # H05 检索是唯一允许的长超时点位
    longest = max(HOOK_POINT_SPECS.values(), key=lambda s: s.default_timeout_ms)
    assert longest.point is AgentHookPoint.RETRIEVE_CONTEXT


def test_dispatch_order_sync_fail_open_timeout_and_short_circuit() -> None:
    order: list[str] = []

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
    assert asyncio.run(fire_hooks(AgentHookPoint.COMPOSE_CONTEXT, ctx)) is HookDecision.CONTINUE
    assert order == ["first", "second", "second_b"]

    seen: list[int] = []

    @on_agent_hook(AgentHookPoint.ON_INBOUND, priority=100)
    def sync_hook(ctx: AgentHookContext) -> None:
        seen.append(1)

    @on_agent_hook(AgentHookPoint.ON_INBOUND, priority=200, timeout_ms=30)
    async def slow(ctx: AgentHookContext) -> None:
        await asyncio.sleep(2.0)
        raise AssertionError("should have been cancelled")

    assert asyncio.run(fire_hooks(AgentHookPoint.ON_INBOUND, AgentHookContext(point=AgentHookPoint.ON_INBOUND))) is (
        HookDecision.CONTINUE
    )
    assert seen == [1]

    ran: list[str] = []

    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=100)
    async def boom(ctx: AgentHookContext) -> None:
        raise RuntimeError("kaboom")

    @on_agent_hook(AgentHookPoint.AFTER_RUN, priority=200)
    async def still_runs(ctx: AgentHookContext) -> None:
        ran.append("after")

    assert asyncio.run(fire_hooks(AgentHookPoint.AFTER_RUN, AgentHookContext(point=AgentHookPoint.AFTER_RUN))) is (
        HookDecision.CONTINUE
    )
    assert ran == ["after"]

    assert not hooks_registered(AgentHookPoint.AFTER_ASSEMBLE_TOOLS)
    assert (
        asyncio.run(
            fire_hooks(
                AgentHookPoint.AFTER_ASSEMBLE_TOOLS,
                AgentHookContext(point=AgentHookPoint.AFTER_ASSEMBLE_TOOLS),
            )
        )
        is HookDecision.CONTINUE
    )

    @on_agent_hook(AgentHookPoint.BEFORE_AI_CHAT, priority=100)
    async def abort_it(ctx: AgentHookContext) -> AgentHookResult:
        return ctx.abort("muted")

    @on_agent_hook(AgentHookPoint.BEFORE_AI_CHAT, priority=200)
    async def never(ctx: AgentHookContext) -> None:
        raise AssertionError("must not run after ABORT_RUN")

    abort_ctx = AgentHookContext(point=AgentHookPoint.BEFORE_AI_CHAT)
    assert asyncio.run(fire_hooks(AgentHookPoint.BEFORE_AI_CHAT, abort_ctx)) is HookDecision.ABORT_RUN
    assert abort_ctx.decision_reason == "muted"


def test_capabilities_blocks_and_drop_hooks() -> None:
    ctx = AgentHookContext(point=AgentHookPoint.AFTER_RUN)
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
