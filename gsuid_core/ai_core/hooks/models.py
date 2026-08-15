"""Hook 总线的数据模型：Context（能力票）+ 决策码 + 注册记录。

``AgentHookContext`` **不**把 ``RunOnceState`` 交给任何人。每个点位按
``HOOK_POINT_SPECS`` 声明可写操作，未授权调用抛 ``HookCapabilityError``；
第一方套件与第三方插件走同一套票，否则替换套件时两边 API 对不齐。
"""

from enum import Enum
from typing import TYPE_CHECKING, Set, Dict, List, Tuple, Union, Callable, Optional, Awaitable
from dataclasses import field, dataclass

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.hooks.points import HookPointSpec, AgentHookPoint, HookCapability, spec_for
from gsuid_core.ai_core.hooks.markers import format_hint

if TYPE_CHECKING:
    from gsuid_core.ai_core.relationship import RelationshipView
    from gsuid_core.message_history.manager import MessageRecord
    from gsuid_core.ai_core.relationship.engine import SettleOutcome
    from gsuid_core.ai_core.interaction_scaffold import TurnGraph
    from gsuid_core.ai_core.relationship.signals import TurnSignals


class HookCapabilityError(RuntimeError):
    """套件/插件在无该能力票的点位上调用了受控写操作。dispatcher fail-open 记 warning。"""


class HookDecision(str, Enum):
    """控制码。只认显式返回值，异常一律不升级为 ABORT_RUN。"""

    CONTINUE = "continue"
    ABORT_RUN = "abort_run"
    SILENCE = "silence"
    VETO_TOOL = "veto_tool"


@dataclass
class AgentHookResult:
    """hook 返回值。返回 ``None`` 等价于 ``CONTINUE``。"""

    decision: HookDecision = HookDecision.CONTINUE
    reason: str = ""


@dataclass
class AgentHookContext:
    """单次 fire 的上下文。字段按「身份 / 装配 / 工具 / 出站 / 控制」分组。"""

    point: AgentHookPoint
    # ── 身份 ──
    ev: Optional[Event] = None
    bot: Optional[Bot] = None
    session_id: str = ""
    persona_name: Optional[str] = None
    create_by: str = "Chat"
    is_subagent: bool = False
    is_correction: bool = False
    is_framework: bool = False
    query: str = ""
    # ── 装配 ──
    intent: Optional[str] = None
    blocks: Dict[str, str] = field(default_factory=dict)
    retrieved: Dict[str, str] = field(default_factory=dict)
    hints: List[str] = field(default_factory=list)
    has_actionable: bool = False
    should_speak: bool = True
    # 内核每轮填好、供套件只读的事实（不是套件自己去查库）
    mood_key: str = ""
    soft_triggered: bool = False
    prev_turn_used_tools: bool = False
    recent_report_titles: Tuple[str, ...] = ()
    memory_guide: str = ""
    relationship: Optional["RelationshipView"] = None
    cheap_gate: str = ""
    prior_user_turns: List[str] = field(default_factory=list)
    gate_history: List["MessageRecord"] = field(default_factory=list)
    assembled_domains: List[str] = field(default_factory=list)
    priority_speakers: Set[str] = field(default_factory=set)
    turn_graph: Optional["TurnGraph"] = None
    signals: Optional["TurnSignals"] = None
    settle_outcome: Optional["SettleOutcome"] = None
    enable_system2: Optional[bool] = None
    # ── 工具 ──
    tool_name: Optional[str] = None
    tool_args: Dict[str, object] = field(default_factory=dict)
    tool_return_text: Optional[str] = None
    ensured_tools: List[str] = field(default_factory=list)
    dropped_tools: List[str] = field(default_factory=list)
    addr_gated: bool = False
    # ── 出站 ──
    text: str = ""
    replacement_text: Optional[str] = None
    # ── 控制 ──
    decision: HookDecision = HookDecision.CONTINUE
    decision_reason: str = ""
    correction_requested: bool = False
    correction_reason: str = ""
    # dispatcher 在调用每个 hook 前写入，用于 hint 归属与日志
    current_kit_id: Optional[str] = None

    @property
    def spec(self) -> HookPointSpec:
        return spec_for(self.point)

    def _require(self, cap: HookCapability) -> None:
        if cap not in self.spec.capabilities:
            raise HookCapabilityError(f"{self.point.name} 不提供 {cap.value} 能力票（owner={self.current_kit_id}）")

    # ── 受控写操作 ──

    def set_context_block(self, name: str, text: str) -> None:
        """填一个命名块。``name`` 必须在 ``CONTEXT_BLOCK_ORDER``，未知名丢弃并 warning。"""
        self._require(HookCapability.SET_BLOCK)
        from gsuid_core.ai_core.kits.base import is_known_block

        if not is_known_block(name):
            raise HookCapabilityError(f"未知块名 {name!r}，须在 CONTEXT_BLOCK_ORDER 内")
        body = text.strip()
        if body:
            self.blocks[name] = body
        elif name in self.blocks:
            del self.blocks[name]

    def stash_retrieved(self, name: str, text: str) -> None:
        """H05 检索窗暂存结果，供 H06 写成正式块。不受块名表约束。"""
        self._require(HookCapability.SET_BLOCK)
        self.retrieved[name] = text

    def set_has_actionable(self, flag: bool) -> None:
        self._require(HookCapability.SET_ACTIONABLE)
        self.has_actionable = flag

    def set_intent(self, intent: str) -> None:
        self._require(HookCapability.SET_INTENT)
        if intent not in ("闲聊", "工具", "问答"):
            raise HookCapabilityError(f"intent 只允许 闲聊/工具/问答，实际 {intent!r}")
        self.intent = intent

    def set_should_speak(self, flag: bool) -> None:
        self._require(HookCapability.SET_SHOULD_SPEAK)
        self.should_speak = flag

    def append_user_hint(self, text: str) -> None:
        """追加本轮 user 侧 hint（带来源前缀，不进持久 history）。"""
        self._require(HookCapability.APPEND_HINT)
        hint = format_hint(text, kit_id=self.current_kit_id)
        if hint:
            self.hints.append(hint)

    def ensure_tools(self, *names: str) -> None:
        """钉工具。护栏（已注册名 / 非特权分类 / addr_gated 拒绝）由内核在 fire 后统一执行。"""
        self._require(HookCapability.MUTATE_TOOLS)
        for name in names:
            if name and name not in self.ensured_tools:
                self.ensured_tools.append(name)

    def drop_tools(self, *names: str) -> None:
        self._require(HookCapability.MUTATE_TOOLS)
        for name in names:
            if name and name not in self.dropped_tools:
                self.dropped_tools.append(name)

    def replace_text(self, text: str) -> None:
        self._require(HookCapability.REPLACE_TEXT)
        self.replacement_text = text

    def set_tool_return(self, text: str) -> None:
        self._require(HookCapability.REPLACE_TOOL_RETURN)
        self.tool_return_text = text

    def request_correction(self, reason: str) -> None:
        self._require(HookCapability.REQUEST_CORRECTION)
        self.correction_requested = True
        self.correction_reason = reason

    # ── 控制码 ──

    def abort(self, reason: str = "") -> AgentHookResult:
        self._require(HookCapability.ABORT)
        return AgentHookResult(HookDecision.ABORT_RUN, reason)

    def silence(self, reason: str = "") -> AgentHookResult:
        self._require(HookCapability.SILENCE)
        return AgentHookResult(HookDecision.SILENCE, reason)

    def veto_tool(self, reason: str = "") -> AgentHookResult:
        self._require(HookCapability.VETO_TOOL)
        return AgentHookResult(HookDecision.VETO_TOOL, reason)

    # ── 只读派生 ──

    @property
    def group_id(self) -> Optional[str]:
        """群聊 group_id；私聊恒 None（记忆 scope 红线：私聊禁止回退成 user_id）。"""
        if self.ev is None or not self.ev.group_id:
            return None
        return str(self.ev.group_id)

    @property
    def user_id(self) -> str:
        return str(self.ev.user_id) if self.ev is not None else ""

    @property
    def bot_id(self) -> str:
        if self.bot is not None:
            return self.bot.bot_id
        return str(self.ev.bot_id) if self.ev is not None else ""

    def hint_text(self) -> str:
        return "\n".join(self.hints)


HookFn = Callable[
    ["AgentHookContext"],
    Union[
        Awaitable[Optional[Union[AgentHookResult, HookDecision]]],
        Optional[Union[AgentHookResult, HookDecision]],
    ],
]


@dataclass(frozen=True)
class HookRegistration:
    """一条 hook 注册记录。``order`` 保证同优先级按注册序串行。"""

    point: AgentHookPoint
    func: "HookFn"
    priority: int
    order: int
    module: str
    kit_id: Optional[str]
    create_by: Optional[Tuple[str, ...]]
    personas: Optional[Tuple[str, ...]]
    include_subagent: bool
    include_correction: bool
    include_framework: bool
    timeout_ms: int

    @property
    def label(self) -> str:
        owner = self.kit_id or self.module
        name = self.func.__name__ if hasattr(self.func, "__name__") else repr(self.func)
        return f"{owner}.{name}"

    def matches(self, ctx: AgentHookContext) -> bool:
        """默认过滤器：只跑 Chat/Agent/TEST、跳过子代理 / 纠正轮 / 框架轮。"""
        if self.create_by is not None and ctx.create_by not in self.create_by:
            return False
        if self.personas is not None and (ctx.persona_name or "") not in self.personas:
            return False
        if ctx.is_subagent and not self.include_subagent:
            return False
        if ctx.is_correction and not self.include_correction:
            return False
        if ctx.is_framework and not self.include_framework:
            return False
        return True


DEFAULT_CREATE_BY: Tuple[str, ...] = ("Chat", "Agent", "TEST")
