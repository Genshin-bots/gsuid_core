"""Agent 环 hook 点位枚举（稳定 ID ``Hxx``，套件与第三方插件共用同一张表）。

编号一经发布不再变更：删点只标 deprecated、不复用编号。新增点位追加到末尾。
点位与内核锚点的对应关系见 ``.agents/skills/gscore-development/references/13-agent-loop-hooks.md``。
"""

from enum import Enum
from typing import Dict, Tuple, FrozenSet
from dataclasses import dataclass


class AgentHookPoint(str, Enum):
    """31 个正式点位：入站 1 + 外环 9 + 单次 run 20 + 建 session 1。"""

    # ── 入站（handler.py，生命周期 ①）──
    ON_INBOUND = "H00"

    # ── 外环（handle_ai_chat，④–⑩）──
    BEFORE_AI_CHAT = "H01"
    AFTER_SESSION = "H02"
    CLASSIFY = "H03"
    REACTIVE_GATE = "H04"
    RETRIEVE_CONTEXT = "H05"
    COMPOSE_CONTEXT = "H06"
    AFTER_CONTEXT = "H07"
    AFTER_RUN = "H08"
    ON_AI_ERROR = "H09"

    # ── 内环 A：prepare ──
    BEFORE_RUN = "H10"
    AFTER_BUDGET = "H11"
    AFTER_INIT = "H12"
    AFTER_PREPARE_USER = "H13"

    # ── 内环 B：tools ──
    ASSEMBLE_TOOLS = "H14"
    AFTER_ASSEMBLE_TOOLS = "H15"
    AFTER_BUILD_AGENT = "H15b"

    # ── 内环 C：iter ──
    BEFORE_MODEL_REQUEST = "H16"
    ON_TOOL_RETURN = "H17"
    ON_TOOL_CALL = "H18"
    ON_THINKING = "H19"
    BEFORE_TEXT_GATE = "H20"
    AFTER_TEXT_GATE = "H21"
    AFTER_SEND = "H22"

    # ── 内环 D：settle / 失败 / 清理 ──
    BEFORE_SETTLE = "H23"
    BEFORE_CORRECTION = "H24"
    ON_USAGE_LIMIT = "H25"
    ON_CANCEL = "H26"
    ON_RUN_ERROR = "H27"
    AFTER_CLEANUP = "H28"

    # ── 建 session（唯一允许贡献 system 稳定块的点）──
    ON_STABLE_CONTEXT = "H29"


class HookCapability(str, Enum):
    """能力票：点位声明允许哪些受控写操作，未授权调用抛 HookCapabilityError。"""

    SET_BLOCK = "set_context_block"
    SET_ACTIONABLE = "set_has_actionable"
    SET_INTENT = "set_intent"
    SET_SHOULD_SPEAK = "set_should_speak"
    APPEND_HINT = "append_user_hint"
    MUTATE_TOOLS = "ensure_tools/drop_tools"
    REPLACE_TEXT = "replace_text"
    ABORT = "abort"
    SILENCE = "silence"
    VETO_TOOL = "veto_tool"
    REPLACE_TOOL_RETURN = "set_tool_return"
    REQUEST_CORRECTION = "request_correction"


@dataclass(frozen=True)
class HookPointSpec:
    """点位契约：内核锚点、能力票、默认超时、是否已接线。"""

    point: AgentHookPoint
    anchor: str
    capabilities: FrozenSet[HookCapability]
    default_timeout_ms: int
    wired: bool


def _spec(
    point: AgentHookPoint,
    anchor: str,
    timeout_ms: int,
    *caps: HookCapability,
    wired: bool = True,
) -> HookPointSpec:
    return HookPointSpec(point, anchor, frozenset(caps), timeout_ms, wired)


_C = HookCapability

# 超时预算见计划 §4.7：热路径（H16–H22）50ms，H05 检索 15s 是唯一允许的长超时。
HOOK_POINT_SPECS: Dict[AgentHookPoint, HookPointSpec] = {
    s.point: s
    for s in (
        _spec(AgentHookPoint.ON_INBOUND, "handler.handle_event", 200),
        _spec(AgentHookPoint.BEFORE_AI_CHAT, "handle_ai.handle_ai_chat:budget_passed", 500, _C.ABORT, _C.SILENCE),
        _spec(AgentHookPoint.AFTER_SESSION, "handle_ai.handle_ai_chat:get_ai_session", 500),
        _spec(
            AgentHookPoint.CLASSIFY,
            "handle_ai.handle_ai_chat:intent",
            2000,
            _C.SET_INTENT,
            _C.SET_ACTIONABLE,
            _C.ABORT,
        ),
        _spec(
            AgentHookPoint.REACTIVE_GATE,
            "handle_ai.handle_ai_chat:soft_gate",
            2000,
            _C.SET_SHOULD_SPEAK,
            _C.SILENCE,
        ),
        _spec(AgentHookPoint.RETRIEVE_CONTEXT, "handle_ai.handle_ai_chat:retrieve", 15000, _C.SET_BLOCK),
        _spec(
            AgentHookPoint.COMPOSE_CONTEXT,
            "kits.compose.compose_dynamic_context",
            500,
            _C.SET_BLOCK,
            _C.SET_ACTIONABLE,
        ),
        _spec(AgentHookPoint.AFTER_CONTEXT, "handle_ai.handle_ai_chat:composed", 500, _C.SET_BLOCK, _C.APPEND_HINT),
        _spec(AgentHookPoint.AFTER_RUN, "handle_ai.handle_ai_chat:settle", 500),
        _spec(AgentHookPoint.ASSEMBLE_TOOLS, "agent_run.tools:assemble", 2000, _C.MUTATE_TOOLS),
        _spec(AgentHookPoint.AFTER_ASSEMBLE_TOOLS, "agent_run.tools:after_assemble", 200, _C.MUTATE_TOOLS),
        _spec(AgentHookPoint.ON_STABLE_CONTEXT, "context_assembly.build_session_system_prompt", 500, _C.SET_BLOCK),
        # ── 以下 19 个点位**契约已定、内核尚未开火**（wired=False）──────────────
        # `anchor` 是将来该点位应该落在哪一行，不是「现在已经在那里了」。
        # 标 True 会让插件按发布的表去挂钩子、拿到一个永远不执行的回调且无任何告警；
        # `on_agent_hook` 现在会对未接线点位打 warning。接线时把 wired 改回 True。
        _spec(AgentHookPoint.ON_AI_ERROR, "handle_ai.handle_ai_chat:except", 500, wired=False),
        _spec(AgentHookPoint.BEFORE_RUN, "agent_run.prepare:run_once_state", 200, _C.ABORT, wired=False),
        _spec(AgentHookPoint.AFTER_BUDGET, "agent_run.prepare:budget_passed", 200, wired=False),
        _spec(AgentHookPoint.AFTER_INIT, "agent_run.prepare:tool_context", 200, wired=False),
        _spec(
            AgentHookPoint.AFTER_PREPARE_USER,
            "agent_run.prepare:prepare_user",
            200,
            _C.APPEND_HINT,
            wired=False,
        ),
        _spec(AgentHookPoint.AFTER_BUILD_AGENT, "agent_run.tools:built_agent", 200, wired=False),
        _spec(
            AgentHookPoint.BEFORE_MODEL_REQUEST,
            "agent_run.loop:before_stream",
            50,
            _C.APPEND_HINT,
            wired=False,
        ),
        _spec(
            AgentHookPoint.ON_TOOL_RETURN,
            "agent_run.loop:tool_return",
            500,
            _C.REPLACE_TOOL_RETURN,
            wired=False,
        ),
        _spec(AgentHookPoint.ON_TOOL_CALL, "agent_run.loop:tool_call", 50, _C.VETO_TOOL, wired=False),
        _spec(AgentHookPoint.ON_THINKING, "agent_run.loop:thinking", 50, wired=False),
        _spec(
            AgentHookPoint.BEFORE_TEXT_GATE,
            "agent_run.loop:before_pre_send_gate",
            50,
            _C.REPLACE_TEXT,
            _C.SILENCE,
            wired=False,
        ),
        _spec(AgentHookPoint.AFTER_TEXT_GATE, "agent_run.loop:after_pre_send_gate", 50, wired=False),
        _spec(AgentHookPoint.AFTER_SEND, "agent_run.loop:after_send", 50, wired=False),
        _spec(
            AgentHookPoint.BEFORE_SETTLE,
            "agent_run.settle:before_correction",
            200,
            _C.REQUEST_CORRECTION,
            wired=False,
        ),
        _spec(
            AgentHookPoint.BEFORE_CORRECTION,
            "agent_run.settle:correction",
            200,
            _C.REQUEST_CORRECTION,
            wired=False,
        ),
        _spec(AgentHookPoint.ON_USAGE_LIMIT, "agent_run.settle:usage_limit", 200, _C.APPEND_HINT, wired=False),
        _spec(AgentHookPoint.ON_CANCEL, "agent_run.loop:supersede", 200, wired=False),
        _spec(AgentHookPoint.ON_RUN_ERROR, "agent_run.loop:retry_exhausted", 200, wired=False),
        _spec(AgentHookPoint.AFTER_CLEANUP, "agent_run.loop:finally", 200, wired=False),
    )
}

# 内核真正会开火的点位。第三方挂到其余点位上不会执行——注册时告警而非静默。
WIRED_POINTS: FrozenSet[AgentHookPoint] = frozenset(p for p, s in HOOK_POINT_SPECS.items() if s.wired)

# H29 只在建 session / persona 热更时触发。dispatcher 硬拒非建 session 调用，
# 否则套件把它当每轮 hook 用就会每轮改 system、打光 provider 前缀缓存。
STABLE_CONTEXT_ONLY: Tuple[AgentHookPoint, ...] = (AgentHookPoint.ON_STABLE_CONTEXT,)


def spec_for(point: AgentHookPoint) -> HookPointSpec:
    """取点位契约。枚举与 ``HOOK_POINT_SPECS`` 由本模块内测试锁定为一一对应。"""
    return HOOK_POINT_SPECS[point]
