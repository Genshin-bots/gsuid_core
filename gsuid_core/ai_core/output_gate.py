"""发送前输出闸门执行态容器（GateBag）与统一入口 ``pre_send_gate``。

所有策略判定与重说规则均单向收口于 ``control/policy_ring.py``；本模块只持有会话内
可变计数/熔断状态包（挂在 ``ToolContext.extra["output_gate"]``）、收尾替换规划与薄入口。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Sequence
from dataclasses import field, dataclass
from collections.abc import MutableMapping

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.control import angle_guard as _ab
from gsuid_core.ai_core.control.lines import (
    OOC_JUDGE_MARKER,
    FUND_CLAIM_PREFIX,
    GATE_NUDGE_MARKERS as GATE_NUDGE_MARKERS,
)
from gsuid_core.ai_core.control.directive import GateResult, GateDecision
from gsuid_core.ai_core.control.policy_ring import (
    OocGateHooks,
    AngleGateHooks,
    OutboundEvaluators,
    evaluate_ooc_gate,
    run_outbound_gate,
    evaluate_inner_os_gate,
    evaluate_angle_bracket_gate,
)
from gsuid_core.ai_core.control.outbound_detectors import (
    EXPOSED_TOOLS_EXTRA_KEY,
    OutboundTextView,
    is_ooc_firewall_enabled,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.agent_run.speech_policy import SpeechContext

ExtraMap = MutableMapping[str, object]
_STATE_KEY = "output_gate"


@dataclass
class PolicyState:
    attempts: int = 0
    blocked: list[str] = field(default_factory=list)
    abort: bool = False
    fuse_injected: bool = False
    # 同一 ModelResponse 内多 TextPart 只计 1 次 attempt
    batch_counted: bool = False


@dataclass
class GateBag:
    angle_bracket: PolicyState = field(default_factory=PolicyState)
    ooc: PolicyState = field(default_factory=PolicyState)
    inner_os: PolicyState = field(default_factory=PolicyState)
    ooc_warned_turn_ids: set[str] = field(default_factory=set)


def _get_bag(extra: ExtraMap) -> Optional[GateBag]:
    if _STATE_KEY not in extra:
        return None
    raw = extra[_STATE_KEY]
    return raw if isinstance(raw, GateBag) else None


def ensure_gate_bag(extra: ExtraMap) -> GateBag:
    """取得或创建 ``GateBag``；非 bag 实例时覆盖写入。"""
    existing = _get_bag(extra)
    if existing is not None:
        return existing
    bag = GateBag()
    extra[_STATE_KEY] = bag
    return bag


def begin_response_batch(extra: ExtraMap) -> None:
    """每个 ModelResponse 处理 TextPart 前调用：重置「本批已计数」。"""
    bag = ensure_gate_bag(extra)
    bag.angle_bracket.batch_counted = False
    bag.ooc.batch_counted = False
    bag.inner_os.batch_counted = False


def _policy(bag: GateBag, name: str) -> PolicyState:
    if name == "angle_bracket":
        return bag.angle_bracket
    if name == "ooc":
        return bag.ooc
    if name == "inner_os":
        return bag.inner_os
    raise ValueError(f"unknown output_gate policy: {name!r}")


def is_fused(extra: ExtraMap) -> bool:
    """任一策略熔断。"""
    bag = _get_bag(extra)
    return bag is not None and (bag.angle_bracket.abort or bag.ooc.abort)


def mark_fuse_injected(extra: ExtraMap) -> None:
    ensure_gate_bag(extra).angle_bracket.fuse_injected = True


def fuse_already_injected(extra: ExtraMap) -> bool:
    bag = _get_bag(extra)
    return bag is not None and bag.angle_bracket.fuse_injected


def blocked_texts(extra: ExtraMap, policy: str = "angle_bracket") -> List[str]:
    """被某策略拦下的原文列表（历史裁剪用）。"""
    bag = _get_bag(extra)
    if bag is None:
        return []
    return [x.strip() for x in _policy(bag, policy).blocked if x.strip()]


def attempt_count(extra: ExtraMap, policy: str = "angle_bracket") -> int:
    bag = _get_bag(extra)
    return _policy(bag, policy).attempts if bag is not None else 0


def _record_block(extra: ExtraMap, policy: str, text: str, *, count_attempt: bool) -> int:
    bag = ensure_gate_bag(extra)
    ps = _policy(bag, policy)
    stripped = text.strip()
    if stripped:
        ps.blocked.append(stripped)
    if count_attempt and not ps.batch_counted:
        ps.attempts += 1
        ps.batch_counted = True
    return ps.attempts


def set_fused(extra: ExtraMap, policy: str = "angle_bracket") -> None:
    """收尾路径显式熔断（与环内 FUSE 对齐）。"""
    _policy(ensure_gate_bag(extra), policy).abort = True


def _extra_turn_id(extra: ExtraMap) -> str:
    if "turn_id" not in extra or extra["turn_id"] is None:
        return ""
    return str(extra["turn_id"])


def mark_ooc_reminded(extra: ExtraMap) -> None:
    """系统提醒已交给模型（注入下一轮或工具 return）后调用。"""
    turn_id = _extra_turn_id(extra)
    if turn_id:
        ensure_gate_bag(extra).ooc_warned_turn_ids.add(turn_id)


def ooc_reminder_delivered(extra: ExtraMap) -> bool:
    turn_id = _extra_turn_id(extra)
    return bool(turn_id) and turn_id in ensure_gate_bag(extra).ooc_warned_turn_ids


def is_ooc_judge_feedback(text: str) -> bool:
    return bool(text) and (OOC_JUDGE_MARKER in text or text.startswith(FUND_CLAIM_PREFIX))


# ── 收尾规划 ─────────────────────────────────────────────────────────


@dataclass
class AfterRunAnglePlan:
    """run 结束后尖括号策略怎么处理（由 gs_agent 执行发送/改 history）。"""

    fused: bool
    blocked: list[str]
    attempts: int
    replace_map: dict[str, str] = field(default_factory=dict)
    rewrite_original: str = ""
    scrub_nudges: bool = True


def _build_angle_replace_map(blocked: Sequence[str], cleaned: Sequence[str]) -> dict[str, str]:
    if not blocked or not cleaned:
        return {}
    clean_last = cleaned[-1]
    if len(blocked) == 1:
        return {blocked[0]: clean_last}
    if len(blocked) == len(cleaned):
        return {b: c for b, c in zip(blocked, cleaned) if b and c}
    return {blocked[-1]: clean_last}


def plan_angle_after_run(extra: ExtraMap, *, clean_sent: Sequence[str]) -> AfterRunAnglePlan:
    """根据 GateBag 与本轮已发干净文本，规划尖括号收尾。"""
    fused = is_fused(extra)
    blocked = blocked_texts(extra, "angle_bracket")
    attempts = attempt_count(extra, "angle_bracket")
    if fused:
        return AfterRunAnglePlan(fused=True, blocked=blocked, attempts=attempts, scrub_nudges=True)
    if not blocked:
        return AfterRunAnglePlan(fused=False, blocked=[], attempts=attempts, scrub_nudges=bool(attempts))
    cleaned = [t for t in clean_sent if t and not _ab.has_illegal_angle_tags(t)]
    if cleaned:
        return AfterRunAnglePlan(
            fused=False,
            blocked=blocked,
            attempts=attempts,
            replace_map=_build_angle_replace_map(blocked, cleaned),
            scrub_nudges=True,
        )
    return AfterRunAnglePlan(
        fused=False,
        blocked=blocked,
        attempts=attempts,
        rewrite_original=blocked[-1],
        scrub_nudges=True,
    )


def merge_rewrite_feedbacks(feedbacks: Sequence[str]) -> str:
    """同一 ModelResponse 多段 REWRITE 合成一条注入文案。"""
    parts = [f.strip() for f in feedbacks if f and f.strip()]
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 else "\n\n---\n\n".join(parts)


# ── 公开入口（将 GateBag 状态钩子接到 control.policy_ring）────────────────


def _eval_angle(view: OutboundTextView, extra: ExtraMap, *, channel: str, count_attempt: bool) -> Optional[GateResult]:
    return evaluate_angle_bracket_gate(
        view,
        channel=channel,
        hooks=AngleGateHooks(
            is_fused=lambda: is_fused(extra),
            record_block=lambda raw: _record_block(extra, "angle_bracket", raw, count_attempt=count_attempt),
            set_abort=lambda: set_fused(extra, "angle_bracket"),
        ),
    )


def _eval_ooc(view: OutboundTextView, extra: ExtraMap, *, user_text: str, channel: str) -> Optional[GateResult]:
    fw_on = is_ooc_firewall_enabled()
    names: list[str] = []
    if fw_on and EXPOSED_TOOLS_EXTRA_KEY in extra:
        raw_names = extra[EXPOSED_TOOLS_EXTRA_KEY]
        if isinstance(raw_names, (list, tuple, set, frozenset)):
            names = [str(x) for x in raw_names if str(x)]
    return evaluate_ooc_gate(
        view.raw,
        user_text=user_text,
        channel=channel,
        exposed_tool_names=names,
        view=view,
        only_always_enforced=not fw_on,
        hooks=OocGateHooks(
            is_reminded=lambda: ooc_reminder_delivered(extra),
            mark_reminded=lambda: mark_ooc_reminded(extra),
            set_abort_ooc=lambda: set_fused(extra, "ooc"),
        ),
    )


def _eval_inner_os(view: OutboundTextView, extra: ExtraMap, *, channel: str, user_text: str) -> Optional[GateResult]:
    def _record_inner(body: str) -> int:
        bag = ensure_gate_bag(extra)
        bag.inner_os.attempts += 1
        if body:
            bag.inner_os.blocked.append(body)
        return bag.inner_os.attempts

    def _recheck(rest_view: OutboundTextView) -> Optional[GateResult]:
        r = _eval_angle(rest_view, extra, channel=channel, count_attempt=False)
        if r is not None and r.decision is not GateDecision.ALLOW:
            return r
        return _eval_ooc(rest_view, extra, user_text=user_text, channel=channel)

    return evaluate_inner_os_gate(view, record_attempt=_record_inner, recheck_rest=_recheck)


def pre_send_gate(
    text: str,
    extra: Optional[ExtraMap] = None,
    *,
    user_text: str = "",
    channel: str = "main",
    count_attempt: bool = True,
    is_group: bool = False,
    speech_ctx: "SpeechContext | None" = None,
) -> GateResult:
    """对即将发出的 agent 台词做发送前闸门（由 ``control.policy_ring`` 单表执行）。"""
    if channel not in ("main", "tool"):
        logger.warning(i18n_t("log.ai.output_gate_unknown_channel", channel=repr(channel)))
        channel = "main"
    bag_extra: ExtraMap = extra if extra is not None else {}
    return run_outbound_gate(
        text,
        channel=channel,
        is_group=is_group,
        speech_ctx=speech_ctx,
        evaluators=OutboundEvaluators(
            angle_bracket=lambda v: _eval_angle(v, bag_extra, channel=channel, count_attempt=count_attempt),
            ooc=lambda v: _eval_ooc(v, bag_extra, user_text=user_text, channel=channel),
            inner_os=lambda v: _eval_inner_os(v, bag_extra, channel=channel, user_text=user_text),
        ),
    )
