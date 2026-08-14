"""控制面原语：Directive / Obligation / Evidence 与 ``<control>`` 信封渲染。

设计要点（见 docs/AI_CONTROL_PLANE_UNIFICATION_20260814.md §2）：

* ``observation`` 只允许陈述框架**可验证**的事实；启发式判断须降级为 advisory。
* 有未履行义务时，合法出口只有 *satisfy* 与 *dispute*——**不含空操作**。
  这正是生产事故的成因：旧 nudge 把「立即 create_subagent」与「只可 <SILENCE>」
  放进同一许可集，观察为假时模型只能选空操作。
* ``reason_code`` 只进日志与统计，**不渲染给模型**（防被当业务语义复述）。
"""

from __future__ import annotations

from typing import Literal, Mapping, Sequence
from dataclasses import field, dataclass

DirectiveKind = Literal["correction", "delivery", "advisory", "terminal"]
ObligationKind = Literal["call_tool", "deliver", "check_delegation"]

# 信封标签：与 read_image 的 <untrusted source=…> 对称——入站不可信内容已有边界，
# 入站**权威**内容也须有；模型据此区分控制面与群友发言。
CONTROL_ENVELOPE_TAG = "control"

# ToolContext.extra 键：本轮申辩理由。定义在此叶子模块，供 prepare 与工具层共用
# 而不互相 import（prepare → buildin_tools 会绕出循环依赖）。
DISPUTE_EXTRA_KEY = "directive_disputes"

# 义务履行的结构事实名（由 settle 从 RunOnceState 派生，非文本判定）
_SATISFACTION_FACTS = frozenset(
    {
        "image_sent",
        "render_delegated",
        "status_tool_called",
        "any_tool_called",
        "delegation_checked",
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


def known_satisfaction_facts() -> frozenset[str]:
    return _SATISFACTION_FACTS


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


def disputes_of(extra: Mapping[str, object]) -> list[str]:
    """读取本轮申辩理由（settle / 观测用）。"""
    bucket = extra[DISPUTE_EXTRA_KEY] if DISPUTE_EXTRA_KEY in extra else None
    if not isinstance(bucket, list):
        return []
    return [item for item in bucket if isinstance(item, str)]
