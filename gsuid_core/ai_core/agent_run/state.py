"""单次 run 的可变状态袋。"""

from __future__ import annotations

from typing import Any, Union, Literal, Sequence
from dataclasses import field, dataclass

from pydantic_ai.usage import UsageLimits
from pydantic_ai.messages import UserContent

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.ai_core import output_firewall
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.rag.tools import ToolList

ReturnMode = Literal["always", "return", "by_bot"]

# 预算闸门放行哨兵（与超额早退的 None/"" 区分）
BUDGET_GATE_PASS = object()
# 兼容旧名
_BUDGET_GATE_PASS = BUDGET_GATE_PASS


@dataclass
class RunOnceState:
    """``_execute_run_once`` 单次尝试的共享可变状态。"""

    # 入参镜像
    user_message: Union[str, Sequence[UserContent]]
    bot: Bot | None
    ev: Event | None
    rag_context: str | None
    tools: ToolList
    return_mode: ReturnMode
    output_type: type[Any] | None
    intent: str | None
    has_active_task: bool
    budget_gate: bool
    suppress_intermediate_text: bool
    fake_done_retry: bool
    turn_graph: Any | None
    cheap_gate: Any | None
    is_framework_injection: bool

    # 预算
    budget_scope: tuple[str, str, str] | None = None
    budget_scope_token: Any | None = None

    # 环内可变
    tool_call_list: list[str] = field(default_factory=list)
    wall_nudged: bool = False
    ooc_blocked: list[tuple[str, output_firewall.FirewallHit]] = field(default_factory=list)
    ab_pending_nudges: list[str] = field(default_factory=list)
    ab_abort: bool = False
    fab_blocked: list[str] = field(default_factory=list)
    saw_structured_return: bool = False
    delegated_render: bool = False
    same_tool_streak: int = 0
    same_tool_name: str = ""
    thrash_fused: bool = False
    thinking_segments: list[str] = field(default_factory=list)
    generation_cancelled: bool = False
    cancel_ev: Any | None = None
    # 出站话术：free / silence_only / status_ok / framework_nudge / framework_deliver
    speech_policy: str = "free"
    status_inquiry: bool = False
    pending_async_delivery: bool = False
    image_sent_this_run: bool = False
    has_status_tool_call: bool = False
    # 本轮曾拦截「报告体」台词 → settle 强制 render 纠正
    report_speech_blocked: bool = False
    # 本轮已发过一句等待安慰（出图前）
    wait_comfort_sent: bool = False

    # 时钟 / 限额
    limits: UsageLimits | None = None
    start_time: float = 0.0
    wall_acc: Any | None = None
    wall_clock_token: Any | None = None
    turn_id: str = ""
    # 用户回合：root 主人格生成并写入 contextvar；嵌套 run 继承。
    user_turn_id: str = ""
    owns_user_turn: bool = False
    user_turn_token: Any | None = None

    # 上下文 / 用户消息
    blocked_exclusive: set[str] = field(default_factory=set)
    allow_outbound: bool = False
    run_extra: dict[str, Any] = field(default_factory=dict)
    fw_msg: bool = False
    context: ToolContext | None = None
    last_user_question: str = ""
    final_user_message: Union[str, list[UserContent]] = ""
    lean_user_message: Union[str, list[UserContent]] = ""

    # 脚手架
    addr_gated: bool = False
    followup_detected: bool = False
    tg: Any | None = None
    cheap: Any | None = None
    is_light: bool = False
    has_media: bool = False
    group_slim: bool = False

    # 工具
    expose_dynamic: bool = False
    tool_names: list[str] = field(default_factory=list)

    # 流式统计 / 模型
    req_start: float = 0.0
    first_event_at: float | None = None
    last_event_at: float | None = None
    model_name: str = "unknown"
    provider: str = "unknown"
    thinking_tags: tuple[str, str] = ("think", "think")


def _require_context(st: RunOnceState) -> ToolContext:
    """init_state 之后 context 必有；类型收窄给 pyright。"""
    ctx = st.context
    assert ctx is not None
    return ctx


def _require_limits(st: RunOnceState) -> UsageLimits:
    lim = st.limits
    assert lim is not None
    return lim
