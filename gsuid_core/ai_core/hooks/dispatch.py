"""Hook 分发器：串行执行 + 超时 + fail-open。

三条纪律（计划 §4.7 / §5.1）：
1. 空表立刻 CONTINUE，不建 Context；
2. 单个 hook 异常吞掉 + ``logger.warning``，**不得**升级成 ABORT_RUN、不得变成人格台词；
3. 控制码只认显式返回值。
"""

import asyncio
from typing import Optional, Awaitable

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.hooks.models import HookFn, HookDecision, AgentHookResult, AgentHookContext, HookCapabilityError
from gsuid_core.ai_core.hooks.points import STABLE_CONTEXT_ONLY, AgentHookPoint
from gsuid_core.ai_core.hooks.registry import hooks_for, is_async_hook, hooks_registered


def hooks_enabled() -> bool:
    """总闸 ``agent_hooks_enable``；AI 总开关关闭时总线整条不跑（D-21）。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        return False
    return bool(ai_config.get_config("agent_hooks_enable").data)


def should_fire(point: AgentHookPoint) -> bool:
    """内核在建 Context 之前调用：无 hook / 总闸关 → 省掉整段准备工作。"""
    return hooks_registered(point) and hooks_enabled()


async def fire_hooks(
    point: AgentHookPoint,
    ctx: AgentHookContext,
    *,
    stable_context_phase: bool = False,
) -> HookDecision:
    """按序执行该点位全部 hook，返回最终控制码（首个非 CONTINUE 即短路）。

    ``stable_context_phase``：只有 ``build_session_system_prompt`` 传 True。
    H29 在非建 session 阶段被硬拒，防套件把它当每轮 hook 用而每轮改 system。
    """
    if not hooks_enabled():
        return HookDecision.CONTINUE
    if point in STABLE_CONTEXT_ONLY and not stable_context_phase:
        logger.warning(t("log.agent.hooks_stable_point_rejected", point=point.name))
        return HookDecision.CONTINUE

    # 同一 Context 跨点位复用：能力票以本次 fire 的点位为准。
    ctx.point = point
    regs = [r for r in hooks_for(point) if r.matches(ctx)]
    if not regs:
        return HookDecision.CONTINUE
    logger.debug(t("log.agent.hooks_fire_point", point=point.name, n=len(regs)))

    for reg in regs:
        ctx.current_kit_id = reg.kit_id
        result = await _invoke(reg.func, ctx, reg.timeout_ms, reg.label, point)
        if result is not None and result.decision is not HookDecision.CONTINUE:
            ctx.decision = result.decision
            ctx.decision_reason = result.reason
            logger.info(
                t(
                    "log.agent.hooks_decision_short_circuit",
                    point=point.name,
                    owner=reg.label,
                    decision=result.decision.value,
                )
            )
            ctx.current_kit_id = None
            return result.decision
    ctx.current_kit_id = None
    return HookDecision.CONTINUE


async def _invoke(
    func: HookFn,
    ctx: AgentHookContext,
    timeout_ms: int,
    label: str,
    point: AgentHookPoint,
) -> Optional[AgentHookResult]:
    """单个 hook 的执行壳：同步函数走线程、超时与异常一律 fail-open。"""
    if not callable(func):
        return None
    timeout = timeout_ms / 1000.0
    try:
        if is_async_hook(func):
            coro = func(ctx)
            if not isinstance(coro, Awaitable):
                return None
            raw = await asyncio.wait_for(coro, timeout=timeout)
        else:
            raw = await asyncio.wait_for(asyncio.to_thread(func, ctx), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(t("log.agent.hooks_timeout", point=point.name, owner=label, ms=timeout_ms))
        return None
    except HookCapabilityError as e:
        logger.warning(t("log.agent.hooks_capability_denied", point=point.name, owner=label, e=e))
        return None
    except Exception as e:
        logger.warning(t("log.agent.hooks_fail", point=point.name, owner=label, e=e))
        return None
    if isinstance(raw, AgentHookResult):
        return raw
    if isinstance(raw, HookDecision):
        return AgentHookResult(raw)
    return None
