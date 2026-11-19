"""Agent 环 hook 点位枚举（稳定 ID ``Hxx``，套件与第三方插件共用同一张表）。

编号一经发布不再变更：删点只标 deprecated、不复用编号。新增点位追加到末尾。
点位与内核锚点的对应关系见 ``.agents/skills/gscore-development/references/13-agent-loop-hooks.md``。
"""

from enum import Enum
from typing import Dict, Tuple, FrozenSet
from dataclasses import dataclass

from gsuid_core.ai_core.classifier.turn_router import ROUTER_TIMEOUT_S


class AgentHookPoint(str, Enum):
    """11 个已接线点位：入站 1 + 外环 7 + 工具装配 2 + 建 session 1。"""

    # ── 入站（handler.py）──
    ON_INBOUND = "H00"

    # ── 外环（handle_inbox_turn）──
    BEFORE_AI_CHAT = "H01"
    AFTER_SESSION = "H02"
    CLASSIFY = "H03"
    RETRIEVE_CONTEXT = "H05"
    COMPOSE_CONTEXT = "H06"
    AFTER_CONTEXT = "H07"
    AFTER_RUN = "H08"

    # ── 内环：工具装配（agent_run/tools.py）──
    ASSEMBLE_TOOLS = "H14"
    AFTER_ASSEMBLE_TOOLS = "H15"

    # ── 建 session（唯一允许贡献 system 稳定块的点）──
    ON_STABLE_CONTEXT = "H29"


class HookDecision(str, Enum):
    """控制码。只认显式返回值，异常一律不升级为 ABORT_RUN。"""

    CONTINUE = "continue"
    ABORT_RUN = "abort_run"
    SILENCE = "silence"


class HookCapability(str, Enum):
    """能力票：点位声明允许哪些受控写操作，未授权调用抛 HookCapabilityError。"""

    SET_BLOCK = "set_context_block"
    SET_ACTIONABLE = "set_has_actionable"
    SET_INTENT = "set_intent"
    APPEND_HINT = "append_user_hint"
    MUTATE_TOOLS = "ensure_tools/drop_tools"
    ABORT = "abort"
    SILENCE = "silence"


@dataclass(frozen=True)
class HookPointSpec:
    """点位契约：内核锚点、能力票、默认超时、是否已接线、超时决断。"""

    point: AgentHookPoint
    anchor: str
    capabilities: FrozenSet[HookCapability]
    default_timeout_ms: int
    wired: bool = True
    on_timeout_decision: HookDecision = HookDecision.CONTINUE


def _spec(
    point: AgentHookPoint,
    anchor: str,
    timeout_ms: int,
    *caps: HookCapability,
    on_timeout_decision: HookDecision = HookDecision.CONTINUE,
) -> HookPointSpec:
    return HookPointSpec(point, anchor, frozenset(caps), timeout_ms, True, on_timeout_decision)


_C = HookCapability

# H05 检索 15s 是唯一允许的长超时，其余点位控制在 200ms–2s。
HOOK_POINT_SPECS: Dict[AgentHookPoint, HookPointSpec] = {
    s.point: s
    for s in (
        _spec(AgentHookPoint.ON_INBOUND, "handler.handle_event", 200),
        _spec(AgentHookPoint.BEFORE_AI_CHAT, "handle_ai.handle_inbox_turn:budget_passed", 500, _C.ABORT, _C.SILENCE),
        _spec(AgentHookPoint.AFTER_SESSION, "handle_ai.handle_inbox_turn:get_ai_session", 500),
        # 预算必须盖过 TurnRouter 自身超时，否则软批被整批静默。
        _spec(
            AgentHookPoint.CLASSIFY,
            "handle_ai.handle_inbox_turn:intent",
            int(ROUTER_TIMEOUT_S * 1000) + 1000,
            _C.SET_INTENT,
            _C.SET_ACTIONABLE,
            _C.ABORT,
        ),
        _spec(AgentHookPoint.RETRIEVE_CONTEXT, "handle_ai.handle_inbox_turn:retrieve", 15000, _C.SET_BLOCK),
        _spec(
            AgentHookPoint.COMPOSE_CONTEXT,
            "kits.compose.compose_dynamic_context",
            500,
            _C.SET_BLOCK,
            _C.SET_ACTIONABLE,
        ),
        _spec(AgentHookPoint.AFTER_CONTEXT, "handle_ai.handle_inbox_turn:composed", 500, _C.SET_BLOCK, _C.APPEND_HINT),
        _spec(AgentHookPoint.AFTER_RUN, "handle_ai.handle_inbox_turn:settle", 500),
        _spec(AgentHookPoint.ASSEMBLE_TOOLS, "agent_run.tools:assemble", 2000, _C.MUTATE_TOOLS),
        _spec(AgentHookPoint.AFTER_ASSEMBLE_TOOLS, "agent_run.tools:after_assemble", 200, _C.MUTATE_TOOLS),
        _spec(AgentHookPoint.ON_STABLE_CONTEXT, "context_assembly.build_session_system_prompt", 500, _C.SET_BLOCK),
    )
}

# H29 只在建 session / persona 热更时触发。dispatcher 硬拒非建 session 调用，
# 否则套件把它当每轮 hook 用就会每轮改 system、打光 provider 前缀缓存。
STABLE_CONTEXT_ONLY: Tuple[AgentHookPoint, ...] = (AgentHookPoint.ON_STABLE_CONTEXT,)


def spec_for(point: AgentHookPoint) -> HookPointSpec:
    """取点位契约。枚举与 ``HOOK_POINT_SPECS`` 由本模块内测试锁定为一一对应。"""
    return HOOK_POINT_SPECS[point]
