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
    #: 出站是否按 delta 推。与 pydantic-ai node.stream（TTFT/TPS）正交。
    outbound_stream: bool = False
    #: Token 用量分类；空则 settle 用 create_by。HTTP 入口写 Http_Chat。
    stats_chat_type: str = ""

    # 预算
    budget_scope: tuple[str, str, str] | None = None
    budget_scope_token: Any | None = None

    # 环内可变
    tool_call_list: list[str] = field(default_factory=list)
    effectual_mutate: bool = False
    wall_nudged: bool = False
    ooc_blocked: list[tuple[str, output_firewall.FirewallHit]] = field(default_factory=list)
    ab_pending_nudges: list[str] = field(default_factory=list)
    ab_abort: bool = False
    fab_blocked: list[str] = field(default_factory=list)
    # 出处凭据：**只能**由真实 ToolReturnPart 置位（INV-1）。
    # 排版形状永不构成「有事实包」的证据，否则纯文本长回答会被误判成待出图数据。
    saw_structured_return: bool = False
    delegated_render: bool = False
    same_tool_streak: int = 0
    same_tool_name: str = ""
    thrash_fused: bool = False
    thinking_segments: list[str] = field(default_factory=list)
    #: 本 ModelRequest 已从流式 event 推过 thinking_delta，CallTools 勿再整段重放
    thinking_streamed: bool = False
    #: 本 ModelRequest 流式已见函数 ToolCall；中间 OS 不再入出站流
    stream_saw_fn_tool: bool = False
    generation_cancelled: bool = False
    cancel_ev: Any | None = None
    # 出站话术：free / silence_only / status_ok / framework_nudge /
    # framework_deliver / delivered（终局沉默，见 speech_policy）
    speech_policy: str = "free"
    status_inquiry: bool = False
    pending_async_delivery: bool = False
    image_sent_this_run: bool = False
    # 交付终局：send_message_by_ai 已带台词成功交付 → 本 run 对用户只许 SILENCE。
    # 由工具回执确认后置位（非工具调用时），防失败回执误入终局。
    delivered_terminal: bool = False
    # 终局 SILENCE 指令是否已注入过 ModelRequest（每 run 至多一次）
    delivered_nudged: bool = False
    has_status_tool_call: bool = False
    # 排版失配：台词呈长结构被拦。**纯呈现问题**，不得据此强制工具或销毁内容
    # （用户可能就是要长文本）。与 saw_structured_return 正交，见 INV-1/INV-3。
    presentation_mismatch: bool = False
    # 被排版闸暂扣、从未出站的台词原文。纠正被申辩或未产出替代品时须真发出去，
    # 否则 by_bot 路径 return "" 会让整轮零输出（INV-4）。
    presentation_withheld: list[str] = field(default_factory=list)
    # 与 withheld 一一对应的拦因（仅 report_speech / empty_handoff 可武装出图义务）。
    presentation_withheld_reasons: list[str] = field(default_factory=list)
    # 本轮已暴露给模型的工具名（装配池 ∪ find_tools），供台词标识符泄漏检测。
    exposed_tool_names: list[str] = field(default_factory=list)
    # 本轮见过「无时点聚合」工具返回（气候/月均/历史均值）→ 台词禁冒充实时读数
    saw_timeless_aggregate: bool = False
    # 时效账本（方案七）：web 滞后 / as_of 新鲜 / 其它成功非 web 返回。
    # 仅「有 web 且无 as_of 且无非 web 数据」才注入 WEB_ONLY_STALENESS_CAVEAT。
    saw_web_source: bool = False
    saw_fresh_data: bool = False
    saw_non_web_data: bool = False
    # 本轮已发过一句等待安慰（出图前）
    wait_comfort_sent: bool = False
    # 有活跃任务且本轮真人句很短：瘦检索/语境池，保住委派查询工具。
    in_flight_short: bool = False
    # 出图委派已收到异步 ack / 完成回执；失败回执在未 ack 时回滚抢先静默。
    render_ack_seen: bool = False
    # 主通道已成功发送的台词段数（单轮出站配额兜底，见 4.10）
    main_channel_sends: int = 0
    # 本 run 已见过几个「含函数 ToolCall」的 ModelResponse（出站槽计数）
    tool_bearing_responses: int = 0
    # 本 run 因出站槽丢掉、须从 new_messages 剥掉的 TextPart 正文
    unsent_texts: list[str] = field(default_factory=list)
    web_search_delegate_nudged: bool = False
    # 无函数工具的终局响应已出站；其后最多再接受一轮闸门 rewrite
    saw_final_response: bool = False
    post_final_requests: int = 0

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
    thinking_tags: tuple[str, str] = ("<think>", "</think>")


def _require_context(st: RunOnceState) -> ToolContext:
    """init_state 之后 context 必有；类型收窄给 pyright。"""
    ctx = st.context
    assert ctx is not None
    return ctx


def _require_limits(st: RunOnceState) -> UsageLimits:
    lim = st.limits
    assert lim is not None
    return lim
