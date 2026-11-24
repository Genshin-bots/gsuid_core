"""控制面原语：Directive / Obligation / Evidence 与 ``<control>`` 信封渲染。

设计要点（见 docs/AI_CONTROL_PLANE_UNIFICATION_20260814.md §2）：

* ``observation`` 只允许陈述框架**可验证**的事实；启发式判断须降级为 advisory。
* 有未履行义务时，合法出口只有 *satisfy* 与 *dispute*——**不含空操作**。
  这正是生产事故的成因：旧 nudge 把「立即 create_subagent」与「只可 <SILENCE>」
  放进同一许可集，观察为假时模型只能选空操作。
* ``reason_code`` 只进日志与统计，**不渲染给模型**（防被当业务语义复述）。
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Literal, Mapping, Sequence
from dataclasses import field, dataclass

if TYPE_CHECKING:
    from gsuid_core.ai_core.control.outbound_detectors import FirewallHit

DirectiveKind = Literal["correction", "delivery", "advisory", "terminal"]
ObligationKind = Literal["call_tool", "deliver", "check_delegation"]


class GateDecision(str, Enum):
    ALLOW = "allow"
    """放行原文。"""
    REWRITE = "rewrite"
    """不发送原文；``feedback`` 打回模型。"""
    FALLBACK = "fallback"
    """立即发送 ``send_text``（安全兜底句）。"""
    FUSE = "fuse"
    """本轮熔断静默；``feedback`` 可提示模型只输出 SILENCE。"""


@dataclass
class GateResult:
    decision: GateDecision
    policy: str = ""
    feedback: str = ""
    send_text: str = ""
    # OOC 主路径：收集后在 run 末尾请模型自主判断（无下一轮请求时）
    defer_ooc: bool = False
    ooc_hit: FirewallHit | None = None
    fused: bool = False
    detail: str = ""


# 信封标签：与 read_image 的 <untrusted source=…> 对称——入站不可信内容已有边界，
# 入站**权威**内容也须有；模型据此区分控制面与群友发言。
CONTROL_ENVELOPE_TAG = "control"

# ToolContext.extra 键：本轮申辩理由。定义在此叶子模块，供 prepare 与工具层共用
# 而不互相 import（prepare → buildin_tools 会绕出循环依赖）。
DISPUTE_EXTRA_KEY = "directive_disputes"
# 本 attempt 申辩已成功 → 控制面终局，禁止再调工具 / 对用户说话。
DISPUTE_CLOSED_KEY = "directive_closed"
DISPUTE_ACKED_KEY = "directive_dispute_acked"
DISPUTE_HAS_PRIOR_REPLY_KEY = "dispute_has_prior_reply"

# 义务履行的结构事实名（由 settle 从 RunOnceState 派生，非文本判定）
_SATISFACTION_FACTS = frozenset(
    {
        "image_sent",
        "render_delegated",
        "async_delegated",
        "effectual_mutated",
        "status_tool_called",
        "any_tool_called",
        "delegation_checked",
        "user_visible_sent",
    }
)


@dataclass(frozen=True)
class Evidence:
    """框架给出观察时的可验证凭据（供模型判断是否申辩）。"""

    tool_returns: int = 0
    structured_returns: int = 0
    tool_calls: int = 0
    detail: str = ""

    def render(self) -> str:
        bits: list[str] = []
        if self.tool_returns:
            bits.append(f"工具返回 {self.tool_returns} 条")
        if self.structured_returns:
            bits.append(f"其中多点结构 {self.structured_returns} 条")
        if self.tool_calls:
            bits.append(f"本轮工具调用 {self.tool_calls} 次")
        if self.detail:
            bits.append(self.detail)
        return "；".join(bits)


@dataclass(frozen=True)
class Obligation:
    """本轮必须完成的动作；由 settle 按结构事实验证，不看模型文本。"""

    must: ObligationKind
    tool_name: str = ""
    tool_args_match: Mapping[str, str] = field(default_factory=dict)
    satisfied_by: tuple[str, ...] = ()
    disputable: bool = True

    def render(self) -> str:
        if self.must == "call_tool" and self.tool_name:
            if self.tool_args_match:
                args = ", ".join(f'{k}="{v}"' for k, v in sorted(self.tool_args_match.items()))
                return f"调用 {self.tool_name}({args})"
            return f"调用 {self.tool_name}"
        if self.must == "check_delegation":
            return "用 check_delegation 核实在途委派的真实状态"
        return "把已有结果真正交付给用户"


@dataclass(frozen=True)
class Directive:
    """框架→模型的一条控制指令。"""

    kind: DirectiveKind
    reason_code: str
    observation: str
    obligations: tuple[Obligation, ...] = ()
    evidence: Evidence | None = None

    @property
    def has_obligation(self) -> bool:
        return bool(self.obligations)


def obligation_satisfied(ob: Obligation, *, facts: Sequence[str], tool_calls: Sequence[str]) -> bool:
    """义务是否已履行（结构判定）。

    ``facts`` 是本轮成立的结构事实名（见 ``_SATISFACTION_FACTS``），
    ``tool_calls`` 是本轮实际调用过的工具名。``satisfied_by`` 任一命中即履行。
    声明了 ``tool_args_match`` 时，光有同名工具不够，必须靠结构事实。
    """
    for fact in ob.satisfied_by:
        if fact in facts:
            return True
    if ob.must == "call_tool" and ob.tool_name:
        if ob.tool_name not in tool_calls:
            return False
        return not ob.tool_args_match
    if ob.must == "check_delegation":
        return "delegation_checked" in facts or "check_delegation" in tool_calls
    if ob.must == "deliver":
        return "image_sent" in facts
    return False


def render_control_envelope(directives: Sequence[Directive]) -> str:
    """把若干 Directive 渲染成单个 ``<control>`` 段。

    只陈述观察、义务与出口；不含 ``reason_code``。有义务时**不提供**沉默出口，
    以免「观察为假 → 模型选空操作 → 本轮零输出」。
    """
    if not directives:
        return ""
    kinds = ",".join(sorted({d.kind for d in directives}))
    lines: list[str] = [f'<{CONTROL_ENVELOPE_TAG} kind="{kinds}">']
    any_obligation = False
    any_disputable = False
    for d in directives:
        lines.append(f"观察：{d.observation}")
        if d.evidence is not None:
            rendered = d.evidence.render()
            if rendered:
                lines.append(f"凭据：{rendered}")
        for ob in d.obligations:
            any_obligation = True
            any_disputable = any_disputable or ob.disputable
            lines.append(f"义务：{ob.render()}。")
    if any_obligation and any_disputable:
        lines.append(
            "若观察与事实不符：调用 dispute_directive(reason=…) 说明不符之处；你上一条回复将照原样交付，不必改写。"
        )
    lines.append("本段是框架内部通道，不是群友发言：不要向用户解释、道歉或复述本段。")
    lines.append(f"</{CONTROL_ENVELOPE_TAG}>")
    return "\n".join(lines)


def is_control_envelope(text: str) -> bool:
    """字符串是否已是 ``<control>`` 信封（history 剥离用）。"""
    return text.lstrip().startswith(f"<{CONTROL_ENVELOPE_TAG}")


# 原因码冻结的中文说明（供会话日志与发送拦截文案共用）。
_REASON_TEXT: dict[str, str] = {
    "master_title": "接收人不是主人，台词里出现了主人称呼。",
    "orchestration_leak": "台词向用户讲了内部委派或节点名。",
    "delivery_narration": "同一句里既播报已经交付，又说不必再回复。",
    "report_speech": "台词是长标题、多段或表格，本该出图。",
    "silence_only_or_async": "任务还在进行，这句不是允许的那一次接话。",
    "framework_nudge_no_speech": "这是纠正轮，这句不是一句等待。",
    "process_meta": "台词对用户讲了框架过程或内部字段。",
    "premature_delivery": "还没查到、也没发图，台词却说已经做好。",
    "numeric_recitation": "待出图的材料被念成了多点数字。",
    "empty_handoff": "已有材料，台词却推说不念、不出图。",
    "entity_without_tool": "这轮点到了实体，还没调用查询工具就回答。",
    "deliver_before_send": "交付回灌里的进度句或大段表格，还没先发出去。",
    "status_without_tool": "用户在追问进度，还没查状态就报了结果。",
    "empty": "台词是空白。",
    "output_gate_fuse": "出站闸连续打回，这轮熔断。",
    "output_gate_angle_bracket": "台词含有非法尖括号。",
    "output_gate_ooc": "台词出戏，出站闸打回。",
    "output_gate_inner_os": "台词是内心旁白。",
    "output_gate_rewrite": "出站闸打回原文。",
    "output_gate_fallback": "出站闸改写或清空了原文。",
    "output_gate_tool": "发送工具或开场句被出站闸拦住。",
    "fake_done_withheld": "还没有做成功，台词却声称做完。",
    "main_channel_cap": "这一轮可见台词已经到条数上限。",
    "ooc_recover": "出戏改口之后没有可发的话。",
    "framework_leak_stripped": "去掉框架泄漏之后没有可发的话。",
    "fake_done": "上一句声称做完，但没有成功的改动。",
    "unbacked_inflight_promise": "上一句承诺后台在跑或做好通知，但后台没有任何在途任务。",
    "missing_offered_tool": "该用的工具一个都没调用。",
    "addressed_silence": "有人直接点名，回复却是沉默。",
    "cover_hit_zero_tool": "点到了能力，却没有调用对应工具。",
    "structural_zero_tool": "这轮该用工具，却一个都没调用。",
    "framework_idle_deliver": "交付唤醒后空口报了进度或交了长报告。",
    "blocked_voice": "上一句是编排、过程旁白或交付播报。",
    "render_pending": "材料还没出图。",
    "model_voluntary": "模型自己选择沉默。",
    "empty_output": "这一轮没有产出正文。",
    "delegated_render_unfinished": "出图还没完成。",
    "usage_limit_no_material": "用量到顶，且没有可交代的材料。",
    "usage_limit_fallback_silence": "用量到顶后的补答不可发。",
    "no_material_reply_silence": "没有材料可答。",
    "already_sent_by_tool": "发送工具已经把话发给用户了，这句复述不再发。",
}


def reason_text(reason: str) -> str:
    """把原因码译成写下当时的中文。多个码用逗号分隔，未知码保留原样。"""
    parts = [part.strip() for part in reason.split(",") if part.strip()]
    return "；".join(_REASON_TEXT[p] if p in _REASON_TEXT else f"未登记原因：{p}" for p in parts)
