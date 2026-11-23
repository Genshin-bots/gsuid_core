"""统一策略环：单点收口出站话术、发送前闸门与 settle 结构/出口纠偏。

将原本分散在 ``speech_policy``、``output_gate`` 与 ``agent_run/settle`` 的规则表、
重说指令映射及策略执行循环收敛至此，避免多处并行规则漂移。
"""

from __future__ import annotations

from typing import Optional, Protocol, Sequence
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.utils import is_silence_marker
from gsuid_core.ai_core.output_gate import GateResult, GateDecision
from gsuid_core.ai_core.agent_run.state import ReturnMode, RunOnceState
from gsuid_core.ai_core.agent_run.support import (
    _RENDER_TOOL_NAMES,
    _SCHED_MUTATE_TOOLS,
    _INTERACTIVE_CREATE_BY,
    _MAIN_PERSONA_CREATE_BY,
    _claims_fake_done,
    _claims_missing_offered_tool,
    _claims_inflight_without_backend,
)
from gsuid_core.ai_core.control.directive import Directive, obligation_satisfied
from gsuid_core.ai_core.control.corrections import (
    fake_done_directive,
    master_title_directive,
    blocked_voice_directive,
    inflight_wait_directive,
    premature_claim_directive,
    entity_zero_tool_directive,
    status_zero_tool_directive,
    addressed_silence_directive,
    render_obligation_directive,
    numeric_recitation_directive,
    cover_hit_zero_tool_directive,
    missing_offered_tool_directive,
    structural_zero_tool_directive,
    framework_idle_deliver_directive,
    unbacked_inflight_promise_directive,
)
from gsuid_core.ai_core.agent_run.speech_policy import (
    ZERO_OUTPUT_VOICE_REASONS,
    SpeechContext,
    non_master_title,
)
from gsuid_core.ai_core.control.outbound_detectors import (
    ooc_rule_for,
    keeps_working,
    title_mentioned,
    inspect_outbound_ooc,
    looks_like_process_meta,
    looks_like_wait_comfort,
    looks_like_empty_handoff,
    looks_like_report_speech,
    claims_premature_delivery,
    has_orchestration_narration,
    looks_like_deliver_progress,
    looks_like_numeric_recitation,
    looks_like_task_accept_speech,
    looks_like_delivery_status_narration,
)

# ── A. 话术策略规则 + 配套重说指令（单表合一） ─────────────────────────

_RENDER_OBLIGATION_REASONS: frozenset[str] = frozenset({"report_speech", "empty_handoff", "numeric_recitation"})


@dataclass(frozen=True, slots=True)
class RewriteDirectiveArgs:
    """被话术规则拦截后构造重说 ``Directive`` 所需的最小参数集。"""

    allow_render: bool
    tool_calls: int
    recited_report: bool
    forbid_title: str
    wait_already_sent: bool
    image_sent: bool = False


@dataclass(frozen=True, slots=True)
class SpeechPolicyRule:
    """单条出站话术规则。``block=False`` 明确放行；``rewrite`` 绑定对应重说指令。"""

    reason: str
    match: Callable[[SpeechContext, str], bool]
    block: bool = True
    rewrite: Callable[[RewriteDirectiveArgs], Directive | None] | None = None


def _match_wait_comfort(ctx: SpeechContext, body: str) -> bool:
    active_async = ctx.inflight if ctx.in_settle_exit else (ctx.inflight or ctx.policy == "silence_only")
    if not active_async or ctx.wait_comfort_sent:
        return False
    if ctx.in_settle_exit:
        return looks_like_wait_comfort(body)
    return looks_like_task_accept_speech(body, max_len=ctx.speech_len_hard, user_text=ctx.user_text)


def _match_numeric_recitation(ctx: SpeechContext, body: str) -> bool:
    if ctx.image_sent or not looks_like_numeric_recitation(body):
        return False
    if ctx.in_settle_exit:
        return ctx.allow_render and ctx.fact_pack_pending and bool(ctx.tool_calls_so_far)
    return ctx.renderable_pack and not (ctx.policy == "status_ok" and ctx.has_status_tool)


def _match_report_speech(ctx: SpeechContext, body: str) -> bool:
    if not looks_like_report_speech(body):
        return False
    if ctx.in_settle_exit:
        return ctx.render_obligation and not ctx.skip_report_exit and not ctx.image_sent
    return ctx.policy in ("free", "status_ok", "framework_deliver") and ctx.renderable_pack


def _match_deliver_before_send(ctx: SpeechContext, body: str) -> bool:
    if ctx.in_settle_exit or ctx.policy != "framework_deliver" or ctx.image_sent:
        return False
    if looks_like_deliver_progress(body):
        return True
    if not ctx.allow_render:
        return False
    return (looks_like_report_speech(body) and len(body) >= 160) or (body.count("|") >= 6 and "\n" in body)


def _rw_blocked_voice(_a: RewriteDirectiveArgs) -> Directive:
    return blocked_voice_directive()


def _rw_report_speech(a: RewriteDirectiveArgs) -> Directive:
    if a.image_sent:
        return blocked_voice_directive()
    return render_obligation_directive(recited_report=True, tool_calls=a.tool_calls, allow_render=a.allow_render)


_SPEECH_RING: tuple[SpeechPolicyRule, ...] = (
    SpeechPolicyRule(
        "master_title",
        lambda ctx, body: (not ctx.in_settle_exit) and title_mentioned(ctx.forbid_title, body),
        rewrite=lambda a: master_title_directive(a.forbid_title) if a.forbid_title else None,
    ),
    SpeechPolicyRule(
        "orchestration_leak",
        lambda ctx, body: ctx.image_sent and has_orchestration_narration(body),
        rewrite=_rw_blocked_voice,
    ),
    SpeechPolicyRule(
        "delivery_narration",
        lambda ctx, body: ctx.image_sent and looks_like_delivery_status_narration(body),
        rewrite=_rw_blocked_voice,
    ),
    SpeechPolicyRule(
        "report_speech",
        lambda ctx, body: ctx.in_settle_exit and ctx.image_sent and looks_like_report_speech(body),
        rewrite=_rw_report_speech,
    ),
    SpeechPolicyRule(
        "process_meta",
        lambda ctx, body: (
            ctx.in_settle_exit and ctx.image_sent and looks_like_process_meta(body, user_text=ctx.user_text)
        ),
        rewrite=_rw_blocked_voice,
    ),
    SpeechPolicyRule("post_image_ok", lambda ctx, _body: ctx.image_sent, block=False),
    SpeechPolicyRule("wait_comfort", _match_wait_comfort, block=False),
    SpeechPolicyRule(
        "silence_only_or_async",
        lambda ctx, _body: ctx.inflight or (not ctx.in_settle_exit and ctx.policy == "silence_only"),
        rewrite=lambda a: None if a.wait_already_sent else inflight_wait_directive(),
    ),
    SpeechPolicyRule(
        "wait_comfort",
        lambda ctx, body: (
            (not ctx.in_settle_exit)
            and ctx.policy == "framework_nudge"
            and looks_like_wait_comfort(body)
            and not ctx.wait_comfort_sent
        ),
        block=False,
    ),
    SpeechPolicyRule(
        "framework_nudge_no_speech",
        lambda ctx, _body: (not ctx.in_settle_exit) and ctx.policy == "framework_nudge",
    ),
    SpeechPolicyRule(
        "orchestration_leak",
        lambda _ctx, body: has_orchestration_narration(body),
        rewrite=_rw_blocked_voice,
    ),
    SpeechPolicyRule(
        "delivery_narration",
        lambda _ctx, body: looks_like_delivery_status_narration(body),
        rewrite=_rw_blocked_voice,
    ),
    SpeechPolicyRule(
        "process_meta",
        lambda ctx, body: looks_like_process_meta(body, user_text=ctx.user_text),
        rewrite=_rw_blocked_voice,
    ),
    SpeechPolicyRule(
        "premature_delivery",
        lambda ctx, body: (
            claims_premature_delivery(
                body,
                has_fact_pack=ctx.fact_pack_pending,
                tool_called_this_run=bool(ctx.tool_calls_so_far),
                is_delivery_wake=(ctx.policy == "framework_deliver" or ctx.is_delivery_wake),
            )
            and not ctx.image_sent
            and (ctx.in_settle_exit or not keeps_working(body))
        ),
        rewrite=lambda _a: premature_claim_directive(),
    ),
    SpeechPolicyRule(
        "empty_handoff",
        lambda ctx, body: (
            (ctx.in_settle_exit or ctx.renderable_pack) and not ctx.image_sent and looks_like_empty_handoff(body)
        ),
        rewrite=lambda a: render_obligation_directive(
            recited_report=a.recited_report,
            tool_calls=a.tool_calls,
            allow_render=a.allow_render,
        ),
    ),
    SpeechPolicyRule(
        "numeric_recitation",
        _match_numeric_recitation,
        rewrite=lambda a: numeric_recitation_directive(allow_render=a.allow_render),
    ),
    SpeechPolicyRule("report_speech", _match_report_speech, rewrite=_rw_report_speech),
    SpeechPolicyRule(
        "entity_without_tool",
        lambda ctx, _body: (
            (not ctx.in_settle_exit)
            and ctx.entity_routed
            and not ctx.image_sent
            and not ctx.tool_calls_so_far
            and ctx.policy in ("free", "status_ok")
        ),
        rewrite=lambda _a: entity_zero_tool_directive(),
    ),
    SpeechPolicyRule(
        "deliver_before_send",
        _match_deliver_before_send,
        rewrite=lambda a: framework_idle_deliver_directive(allow_render=a.allow_render),
    ),
    SpeechPolicyRule(
        "status_without_tool",
        lambda ctx, _body: (
            (not ctx.in_settle_exit)
            and ctx.policy == "status_ok"
            and not ctx.has_status_tool
            and not ctx.tool_calls_so_far
        ),
        rewrite=lambda _a: status_zero_tool_directive(),
    ),
)

_REWRITE_BY_REASON: dict[str, Callable[[RewriteDirectiveArgs], Directive | None]] = {
    rule.reason: rule.rewrite for rule in _SPEECH_RING if rule.rewrite is not None
}


def evaluate_speech_ring(ctx: SpeechContext, text: str) -> tuple[bool, str]:
    """按统一话术策略表裁决本段可见文本。返回 ``(block, reason)``。"""
    body = (text or "").strip()
    if not body:
        return True, "empty"
    if is_silence_marker(body):
        return False, "silence"
    for rule in _SPEECH_RING:
        if rule.match(ctx, body):
            return rule.block, rule.reason
    _ = (ctx.speech_len_hard, ctx.user_asked_detail, ctx.has_active_task)
    return False, "ok"


def rewrite_directive_for(
    reason: str,
    *,
    allow_render: bool,
    tool_calls: int,
    recited_report: bool,
    forbid_title: str,
    wait_already_sent: bool,
    image_sent: bool = False,
) -> Directive | None:
    """从同一策略表取出 ``reason`` 对应的重说 ``Directive``。"""
    builder = _REWRITE_BY_REASON[reason] if reason in _REWRITE_BY_REASON else None
    if builder is None:
        return None
    return builder(
        RewriteDirectiveArgs(
            allow_render=allow_render,
            tool_calls=tool_calls,
            recited_report=recited_report,
            forbid_title=forbid_title,
            wait_already_sent=wait_already_sent,
            image_sent=image_sent,
        )
    )


# ── B. 出站发送前策略环（话术策略 → 尖括号 → OOC → 内心戏） ───────────────


@dataclass(frozen=True, slots=True)
class OutboundEvaluators:
    """由 ``output_gate`` 提供的状态化检测器回调（避免策略表直接持有可变包）。"""

    angle_bracket: Callable[[str], Optional[GateResult]]
    ooc: Callable[[str], Optional[GateResult]]
    inner_os: Callable[[str], Optional[GateResult]]


@dataclass(frozen=True, slots=True)
class OutboundGateRule:
    """单条发送前策略条目，非 ALLOW 即短路。"""

    name: str
    evaluate: Callable[[str, Optional[SpeechContext], OutboundEvaluators], Optional[GateResult]]


def _eval_outbound_speech_rule(
    text: str,
    speech_ctx: Optional[SpeechContext],
    _evaluators: OutboundEvaluators,
) -> Optional[GateResult]:
    if speech_ctx is None:
        return None
    blocked, reason = evaluate_speech_ring(speech_ctx, text)
    if blocked:
        return GateResult(decision=GateDecision.REWRITE, policy="speech_policy", detail=reason)
    return None


_OUTBOUND_RING: tuple[OutboundGateRule, ...] = (
    OutboundGateRule("speech_policy", _eval_outbound_speech_rule),
    OutboundGateRule("angle_bracket", lambda t, _sc, ev: ev.angle_bracket(t)),
    OutboundGateRule("ooc", lambda t, _sc, ev: ev.ooc(t)),
    OutboundGateRule("inner_os", lambda t, _sc, ev: ev.inner_os(t)),
)


def run_outbound_gate(
    text: str,
    *,
    channel: str,
    is_group: bool,
    speech_ctx: Optional[SpeechContext],
    evaluators: OutboundEvaluators,
) -> GateResult:
    """出站单句统一闸门环：空值跳过 → 群 PII 脱敏 → 按 ``_OUTBOUND_RING`` 短路裁决。"""
    if not text or not str(text).strip():
        return GateResult(decision=GateDecision.ALLOW)
    if channel not in ("main", "tool"):
        logger.warning(i18n_t("log.ai.output_gate_unknown_channel", channel=repr(channel)))
        channel = "main"
    if is_group:
        from gsuid_core.ai_core.mcp.pii import mask_pii, pii_hits

        hits = pii_hits(text)
        if hits:
            text = mask_pii(text)
            logger.warning(i18n_t("log.ai.output_gate_pii_masked", hits=",".join(hits)))
    for rule in _OUTBOUND_RING:
        res = rule.evaluate(text, speech_ctx, evaluators)
        if res is not None and res.decision is not GateDecision.ALLOW:
            return res
    return GateResult(decision=GateDecision.ALLOW, send_text=text if is_group else "")


# ── B2. 数据驱动 OOC 防火墙裁决环（规则表定义在 control.outbound_detectors） ───


@dataclass(frozen=True, slots=True)
class OocGateHooks:
    """``evaluate_ooc_gate`` 需要回调的 ``output_gate`` 状态动作。"""

    is_reminded: Callable[[], bool]
    mark_reminded: Callable[[], None]
    set_abort_ooc: Callable[[], None]


def evaluate_ooc_gate(
    text: str,
    *,
    user_text: str,
    channel: str,
    exposed_tool_names: Sequence[str],
    hooks: OocGateHooks,
) -> Optional[GateResult]:
    """按 ``_OOC_RING`` 裁决单条出站台词的 OOC 放行 / 重写 / 熔断。"""
    hit = inspect_outbound_ooc(text, user_text=user_text, exposed_tool_names=exposed_tool_names)
    if hit is None:
        return None
    rule = ooc_rule_for(hit.category)
    if rule is None:
        return None
    warn = rule.render_warning(hit)

    if rule.severity == "fuse":
        if channel == "main":
            logger.warning(
                i18n_t(
                    "log.ai.output_gate_drop_text_after_fuse",
                    policy="ooc",
                    preview=repr(text[:80]),
                )
            )
            hooks.set_abort_ooc()
            return GateResult(
                decision=GateDecision.FUSE,
                policy="ooc",
                feedback=warn,
                ooc_hit=hit,
                fused=True,
                detail=hit.category,
            )
        return GateResult(
            decision=GateDecision.REWRITE,
            policy="ooc",
            feedback=warn,
            ooc_hit=hit,
            detail=hit.category,
        )

    if rule.severity == "never_release_defer":
        if hit.category == "machine_dump":
            if channel == "main":
                logger.warning(
                    i18n_t(
                        "log.ai.output_gate_ooc_rewrite_machine_dump_main",
                        preview=repr(text[:80]),
                    )
                )
            else:
                logger.warning(i18n_t("log.ai.output_gate_ooc_rewrite_machine_dump_tool"))
        else:
            logger.warning(
                i18n_t(
                    "log.ai.output_gate_ooc_rewrite_never_release",
                    category=hit.category,
                )
            )
        return GateResult(
            decision=GateDecision.REWRITE,
            policy="ooc",
            feedback=warn,
            defer_ooc=channel == "main",
            ooc_hit=hit,
            detail=hit.category,
        )

    # 软出戏与系统术语：已送达提醒时仅放行主通道自主判断正文；工具通道继续打回。
    if hooks.is_reminded():
        if channel == "main":
            logger.warning(i18n_t("log.ai.output_gate_ooc_allow_after_warn", category=hit.category))
            return GateResult(decision=GateDecision.ALLOW, policy="ooc", detail="judged")
        logger.warning(
            i18n_t(
                "log.ai.output_gate_ooc_rewrite_first_warn",
                category=hit.category,
                matched=repr(hit.matched[:4]),
            )
        )
        return GateResult(
            decision=GateDecision.REWRITE,
            policy="ooc",
            feedback=warn,
            ooc_hit=hit,
            detail=hit.category,
        )

    if channel == "tool":
        hooks.mark_reminded()
        logger.warning(
            i18n_t(
                "log.ai.output_gate_ooc_rewrite_first_warn",
                category=hit.category,
                matched=repr(hit.matched[:4]),
            )
        )
    else:
        logger.warning(
            i18n_t(
                "log.ai.output_gate_ooc_defer",
                category=hit.category,
                matched=repr(hit.matched[:4]),
            )
        )
    return GateResult(
        decision=GateDecision.REWRITE,
        policy="ooc",
        feedback=warn,
        defer_ooc=channel == "main",
        ooc_hit=hit,
        detail=hit.category,
    )


# ── C. 回合结算策略环（前置结构纠偏 + 出口话术裁决 + 单次重说） ───────────────


class SettleRingHost(Protocol):
    """``run_settle_policy_ring`` 依赖的宿主协议（由 ``SettlePhase`` 实现）。"""

    @property
    def persona_name(self) -> str | None: ...

    @property
    def create_by(self) -> str: ...

    @property
    def sent_texts(self) -> set[str]: ...

    @property
    def dispute_count(self) -> int: ...

    def latest_dispute_preview(self) -> str: ...

    @property
    def attempt_tool_calls(self) -> Sequence[str]: ...

    def model_declares_video(self) -> bool: ...

    async def try_correction_pass(
        self,
        st: RunOnceState,
        directives: tuple[Directive, ...],
        *,
        suppress_intermediate_text: bool = True,
        return_mode: ReturnMode = "return",
    ) -> object: ...

    async def emit_unsent_reply(self, st: RunOnceState, text: str) -> None: ...

    async def run_inv3_correction(
        self,
        st: RunOnceState,
        directive: Directive,
        result_msg: str,
        *,
        scrub_replaced: bool = True,
    ) -> str: ...

    def scrub_fake_done_history(self, fabricated: set[str]) -> None: ...

    def note_output_block(self, st: RunOnceState, reason: str, body: str) -> None: ...


@dataclass(frozen=True, slots=True)
class SettleRingInput:
    st: RunOnceState
    result_msg: str
    match_msg: str
    allow_render: bool
    resend_fab_blocked: Callable[[], Awaitable[None]]


@dataclass(slots=True)
class SettleRingOutcome:
    result_msg: str
    skip_report_exit: bool = False
    replacement_visible: bool = False


def turn_user_text(st: RunOnceState) -> str:
    """提取当轮提问文本，用于区分英文正常作答与机器独白。"""
    if st.ev is not None and st.ev.raw_text:
        return st.ev.raw_text
    if st.last_user_question:
        return st.last_user_question
    if isinstance(st.user_message, str):
        return st.user_message
    return ""


def forbid_title_for(st: RunOnceState, persona_name: str | None) -> str:
    raw = st.run_extra["at_user_id"] if "at_user_id" in st.run_extra else None
    uid = str(raw) if isinstance(raw, str) and raw else ""
    if not uid and st.ev is not None and st.ev.user_id:
        uid = str(st.ev.user_id)
    return non_master_title(uid, persona_name)


def correction_is_deliverable(text: str, *, is_delivery_wake: bool = False, user_text: str = "") -> bool:
    """纠正产出是否可直接交付（非沉默、非编排/元叙述脏输出）。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    return not (
        has_orchestration_narration(body)
        or claims_premature_delivery(body, is_delivery_wake=is_delivery_wake)
        or looks_like_report_speech(body)
        or looks_like_process_meta(body, user_text=user_text)
    )


def corrected_or_original(corrected: object, *, original: str, user_text: str = "") -> str:
    """纠正轮结果收敛（INV-3）：无干净产出时保留原答案。"""
    if not isinstance(corrected, str) or not correction_is_deliverable(corrected, user_text=user_text):
        return original
    return corrected.strip()


def _voice_retry_text(corrected: object, *, disputed: bool, blocked: str, user_text: str = "") -> str:
    if disputed:
        body = blocked.strip()
        if body and not is_silence_marker(body):
            return body
        return "<SILENCE>"
    return corrected_or_original(corrected, original="<SILENCE>", user_text=user_text)


def _satisfaction_facts(st: RunOnceState) -> tuple[str, ...]:
    facts: list[str] = []
    if st.image_sent_this_run:
        facts.append("image_sent")
    if st.delegated_render:
        facts.append("render_delegated")
    if st.pending_async_delivery:
        facts.append("async_delegated")
    if st.effectual_mutate:
        facts.append("effectual_mutated")
    if st.has_status_tool_call:
        facts.append("status_tool_called")
    if st.tool_call_list:
        facts.append("any_tool_called")
    if "check_delegation" in st.tool_call_list:
        facts.append("delegation_checked")
    if st.main_channel_sends > 0:
        facts.append("user_visible_sent")
    return tuple(facts)


def _obligations_met(directive: Directive, st: RunOnceState) -> bool:
    facts = _satisfaction_facts(st)
    return all(obligation_satisfied(ob, facts=facts, tool_calls=st.tool_call_list) for ob in directive.obligations)


def _has_unread_attachment(st: RunOnceState, *, video_readable: bool) -> bool:
    ev = st.ev
    if ev is None:
        return False
    quoted_pure_image = bool((ev.reply_id or (st.tg is not None and st.tg.quoted_tome)) and not ev.reply)
    own_imgs = [i for i in ev.image_id_list if i not in ev.reply_image_id_list]
    has_own_img = bool(own_imgs or (ev.image_id and ev.image_id not in ev.reply_image_id_list))
    if has_own_img and not quoted_pure_image:
        return True
    if ev.audio_id_list or ev.audio_id:
        return True
    if ev.file:
        return True
    return bool(video_readable and (ev.video_id_list or ev.video_id))


def _utterance_cover_hit(st: RunOnceState) -> bool:
    if st.ev is None or not st.ev.raw_text:
        return False
    from gsuid_core.ai_core.rag.tools import cover_dominates_utterance

    return cover_dominates_utterance(st.ev.raw_text.strip())


def _zero_tool_needs_correction(st: RunOnceState, *, video_readable: bool) -> bool:
    if _has_unread_attachment(st, video_readable=video_readable):
        return True
    if st.tg is not None and st.tg.call_to_self and st.tg.task_management:
        return True
    if st.followup_detected or (st.tg is not None and st.tg.ellipsis_followup):
        sched_ok = "sched_mutate_ok" in st.run_extra and st.run_extra["sched_mutate_ok"] is True
        if not st.has_active_task and not sched_ok:
            from gsuid_core.ai_core.interaction_scaffold import is_pure_stop_ellipsis

            if is_pure_stop_ellipsis(turn_user_text(st)):
                return False
        return True
    return False


def _voice_block_reason(st: RunOnceState, result_msg: str) -> str:
    if st.main_channel_sends > 0 or st.image_sent_this_run:
        return ""
    if st.pending_async_delivery or st.delegated_render:
        return ""
    if "numeric_recitation" in st.presentation_withheld_reasons:
        return ""
    for reason in st.presentation_withheld_reasons:
        if reason in ZERO_OUTPUT_VOICE_REASONS:
            return reason
    body = (result_msg or "").strip()
    if not body or is_silence_marker(body):
        return ""
    if claims_premature_delivery(body):
        return "premature_delivery"
    if has_orchestration_narration(body) or looks_like_process_meta(body, user_text=turn_user_text(st)):
        return "process_meta"
    return ""


def needs_render_obligation(st: RunOnceState, result_msg: str, *, allow_render: bool = True) -> bool:
    """有出处凭据且台词呈报告体、念数或空交付时才进纠正。"""
    if not allow_render or not st.saw_structured_return or not st.tool_call_list:
        return False
    if looks_like_report_speech(result_msg):
        return True
    if looks_like_empty_handoff(result_msg or ""):
        return True
    if looks_like_numeric_recitation(result_msg or ""):
        return True
    return any(r in _RENDER_OBLIGATION_REASONS for r in st.presentation_withheld_reasons)


@dataclass(slots=True)
class _SettlePreState:
    host: SettleRingHost
    st: RunOnceState
    result_msg: str
    match_msg: str
    allow_render: bool
    render_obligation: bool
    resend_fab_blocked: Callable[[], Awaitable[None]]

    @property
    def interactive_ok(self) -> bool:
        return (not self.st.fake_done_retry) and self.host.create_by in _INTERACTIVE_CREATE_BY

    @property
    def non_capability_interactive(self) -> bool:
        return self.interactive_ok and self.host.create_by != "CapabilityAgent"

    @property
    def non_silence_msg(self) -> bool:
        return bool(self.result_msg) and not is_silence_marker(self.result_msg.strip())

    @property
    def delivery_wake(self) -> bool:
        return bool(
            self.st.fw_msg and "delivery_wake" in self.st.run_extra and self.st.run_extra["delivery_wake"] is True
        )

    @property
    def delivery_wake_needs_correction(self) -> bool:
        if not self.result_msg:
            return False
        dense = self.allow_render and (
            (looks_like_report_speech(self.match_msg) and len(self.result_msg.strip()) >= 160)
            or (self.result_msg.count("|") >= 6 and "\n" in self.result_msg)
        )
        return dense or looks_like_deliver_progress(self.match_msg)


@dataclass(frozen=True, slots=True)
class _SettlePreRule:
    name: str
    match: Callable[[_SettlePreState], bool]
    apply: Callable[[_SettlePreState], Awaitable[SettleRingOutcome]]
    post_fab_allowed: bool = False


async def _apply_fake_done(s: _SettlePreState) -> SettleRingOutcome:
    st = s.st
    logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
    corrected = await s.host.try_correction_pass(
        st,
        (fake_done_directive(tool_pool_size=len(st.tool_names)),),
        suppress_intermediate_text=True,
        return_mode="return",
    )
    if corrected is None and st.fab_blocked and st.bot and st.return_mode in ("always", "by_bot"):
        await s.resend_fab_blocked()
    fabricated = {t.strip() for t in st.fab_blocked}
    if _claims_fake_done(s.match_msg):
        fabricated.add(s.result_msg.strip())
    if isinstance(corrected, str) and correction_is_deliverable(corrected, user_text=turn_user_text(st)):
        next_msg = corrected.strip()
        await s.host.emit_unsent_reply(st, next_msg)
    else:
        next_msg = "<SILENCE>"
    s.host.scrub_fake_done_history(fabricated)
    return SettleRingOutcome(result_msg=next_msg)


async def _apply_delivery_wake(s: _SettlePreState) -> SettleRingOutcome:
    st = s.st
    logger.warning(i18n_t("log.agent.delivery_idle_correction"))
    prior = s.result_msg.strip()
    disputes_before = s.host.dispute_count
    dc = await s.host.try_correction_pass(
        st,
        (framework_idle_deliver_directive(allow_render=s.allow_render),),
        suppress_intermediate_text=True,
        return_mode="return",
    )
    if s.host.dispute_count > disputes_before:
        return SettleRingOutcome(result_msg=prior)
    if isinstance(dc, str) and correction_is_deliverable(dc, is_delivery_wake=True, user_text=turn_user_text(st)):
        return SettleRingOutcome(result_msg=dc.strip())
    return SettleRingOutcome(result_msg="<SILENCE>")


async def _apply_entity_or_cover(s: _SettlePreState) -> SettleRingOutcome:
    st = s.st
    cover_hit = _utterance_cover_hit(st)
    if cover_hit:
        logger.warning(i18n_t("log.agent.cover_hit_zero_tool_correction"))
    else:
        logger.warning(i18n_t("log.agent.entity_zero_tool_correction"))
    prior = s.result_msg.strip()
    disputes_before = s.host.dispute_count
    ec = await s.host.try_correction_pass(
        st,
        (cover_hit_zero_tool_directive() if cover_hit else entity_zero_tool_directive(),),
        suppress_intermediate_text=True,
        return_mode="return",
    )
    disputed = s.host.dispute_count > disputes_before
    called = [n for n in s.host.attempt_tool_calls if n != "dispute_directive"]
    # 命中 cover 的空口建议不能靠申辩留住
    if disputed and not cover_hit:
        next_msg = prior
    elif (
        isinstance(ec, str)
        and correction_is_deliverable(ec, user_text=turn_user_text(st))
        and (called or not cover_hit)
    ):
        next_msg = ec.strip()
    else:
        next_msg = "<SILENCE>"
    if title_mentioned(forbid_title_for(st, s.host.persona_name), next_msg):
        next_msg = "<SILENCE>"
    await s.host.emit_unsent_reply(st, next_msg)
    return SettleRingOutcome(result_msg=next_msg)


async def _apply_master_title(s: _SettlePreState) -> SettleRingOutcome:
    st = s.st
    ban = forbid_title_for(st, s.host.persona_name)
    logger.warning(i18n_t("log.agent.master_title_correction"))
    tc = await s.host.try_correction_pass(
        st,
        (master_title_directive(ban),),
        suppress_intermediate_text=True,
        return_mode="return",
    )
    if (
        isinstance(tc, str)
        and correction_is_deliverable(tc, user_text=turn_user_text(st))
        and not title_mentioned(ban, tc)
    ):
        next_msg = tc.strip()
    else:
        next_msg = "<SILENCE>"
    await s.host.emit_unsent_reply(st, next_msg)
    return SettleRingOutcome(result_msg=next_msg)


async def _apply_voice_block(s: _SettlePreState) -> SettleRingOutcome:
    st = s.st
    voice_reason = _voice_block_reason(st, s.match_msg)
    logger.warning(i18n_t("log.agent.render_data_nudge_once"))
    voice_directive = premature_claim_directive() if voice_reason == "premature_delivery" else blocked_voice_directive()
    blocked = s.result_msg.strip()
    disputes_before = s.host.dispute_count
    vc = await s.host.try_correction_pass(
        st,
        (voice_directive,),
        suppress_intermediate_text=True,
        return_mode="return",
    )
    disputed = s.host.dispute_count > disputes_before
    if disputed:
        logger.info(i18n_t("log.agent.directive_disputed", reason=s.host.latest_dispute_preview()))
    next_msg = _voice_retry_text(vc, disputed=disputed, blocked=blocked, user_text=turn_user_text(st))
    await s.host.emit_unsent_reply(st, next_msg)
    return SettleRingOutcome(result_msg=next_msg)


async def _apply_render_obligation(s: _SettlePreState) -> SettleRingOutcome:
    st = s.st
    logger.warning(i18n_t("log.agent.render_data_nudge_once"))
    if "numeric_recitation" in st.presentation_withheld_reasons:
        directive = numeric_recitation_directive(allow_render=s.allow_render)
    else:
        directive = render_obligation_directive(
            recited_report=looks_like_report_speech(s.match_msg),
            tool_calls=len(st.tool_call_list),
            allow_render=s.allow_render,
        )
    disputes_before = s.host.dispute_count
    sent_before = set(s.host.sent_texts)
    rc = await s.host.try_correction_pass(
        st,
        (directive,),
        suppress_intermediate_text=True,
        return_mode="return",
    )
    disputed = s.host.dispute_count > disputes_before
    orig_before = s.result_msg
    if disputed:
        logger.info(i18n_t("log.agent.directive_disputed", reason=s.host.latest_dispute_preview()))
        s.host.scrub_fake_done_history(set())
        next_msg = s.result_msg
    else:
        next_msg = corrected_or_original(rc, original=s.result_msg, user_text=turn_user_text(st))
        drop_texts: set[str] = set()
        if next_msg.strip() != orig_before.strip():
            drop_texts.add(orig_before.strip())
        s.host.scrub_fake_done_history(drop_texts)
        if not _obligations_met(directive, st):
            logger.info(i18n_t("log.agent.directive_obligation_unmet", code=directive.reason_code))
    skip_report_exit = disputed or not _obligations_met(directive, st)
    adopted = (not disputed) and next_msg.strip() != orig_before.strip()
    nested_visible = any(
        t.strip() and t not in st.presentation_withheld and not looks_like_wait_comfort(t)
        for t in (s.host.sent_texts - sent_before)
    )
    if disputed or next_msg.strip() != orig_before.strip():
        await s.host.emit_unsent_reply(st, next_msg)
    return SettleRingOutcome(
        result_msg=next_msg,
        skip_report_exit=skip_report_exit,
        replacement_visible=adopted or nested_visible,
    )


def _make_inv3_apply(
    build_directive: Callable[[_SettlePreState], Directive],
    *,
    scrub_replaced: bool = True,
) -> Callable[[_SettlePreState], Awaitable[SettleRingOutcome]]:
    async def _run(s: _SettlePreState) -> SettleRingOutcome:
        next_msg = await s.host.run_inv3_correction(
            s.st,
            build_directive(s),
            s.result_msg,
            scrub_replaced=scrub_replaced,
        )
        return SettleRingOutcome(result_msg=next_msg)

    return _run


_SETTLE_PRE_RING: tuple[_SettlePreRule, ...] = (
    _SettlePreRule(
        "fake_done",
        lambda ctx: (
            bool(ctx.result_msg)
            and not ctx.st.effectual_mutate
            and bool(ctx.st.tool_names)
            and ctx.interactive_ok
            and (bool(ctx.st.fab_blocked) or _claims_fake_done(ctx.match_msg))
        ),
        _apply_fake_done,
    ),
    _SettlePreRule(
        "unbacked_inflight_promise",
        lambda ctx: (
            bool(ctx.result_msg)
            and not ctx.st.effectual_mutate
            and not ctx.st.pending_async_delivery
            and not ctx.st.delegated_render
            and not ctx.st.image_sent_this_run
            and bool(ctx.st.tool_names)
            and ctx.non_capability_interactive
            and _claims_inflight_without_backend(ctx.match_msg)
        ),
        _make_inv3_apply(lambda s: unbacked_inflight_promise_directive(tool_calls=len(s.st.tool_call_list))),
    ),
    _SettlePreRule(
        "structural_zero_tool",
        lambda ctx: (
            ctx.non_silence_msg
            and not ctx.st.tool_call_list
            and bool(ctx.st.tool_names)
            and ctx.non_capability_interactive
            and ctx.st.ev is not None
            and _zero_tool_needs_correction(
                ctx.st,
                video_readable=ctx.host.model_declares_video(),
            )
        ),
        _make_inv3_apply(lambda s: structural_zero_tool_directive(tool_pool_size=len(s.st.tool_names))),
    ),
    _SettlePreRule(
        "missing_offered_tool",
        lambda ctx: (
            bool(ctx.result_msg)
            and ctx.non_capability_interactive
            and ctx.st.tg is not None
            and ctx.st.tg.task_management
            and _claims_missing_offered_tool(ctx.match_msg, ctx.st.tool_names)
            and not bool(_SCHED_MUTATE_TOOLS.intersection(ctx.st.tool_call_list))
        ),
        _make_inv3_apply(lambda s: missing_offered_tool_directive(tool_pool_size=len(s.st.tool_names))),
    ),
    _SettlePreRule(
        "addressed_silence",
        lambda ctx: (
            not ctx.non_silence_msg
            and not ctx.host.sent_texts
            and not ctx.st.pending_async_delivery
            and ctx.non_capability_interactive
            and ctx.st.tg is not None
            and ctx.st.tg.call_to_self
            and not ctx.st.tg.quoted_tome
            and not ctx.st.tg.address_gated
        ),
        _make_inv3_apply(lambda _s: addressed_silence_directive(), scrub_replaced=False),
    ),
    _SettlePreRule(
        "status_zero_tool",
        lambda ctx: (
            ctx.st.status_inquiry
            and ctx.st.has_active_task
            and not ctx.st.tool_call_list
            and not ctx.st.fake_done_retry
            and ctx.non_silence_msg
            and ctx.host.create_by in ("Chat", "Agent")
        ),
        _make_inv3_apply(lambda _s: status_zero_tool_directive(), scrub_replaced=False),
    ),
    _SettlePreRule(
        "delivery_wake",
        lambda ctx: (
            ctx.delivery_wake
            and not ctx.st.fake_done_retry
            and not ctx.st.tool_call_list
            and not ctx.st.image_sent_this_run
            and ctx.non_silence_msg
            and ctx.host.create_by in ("Chat", "Agent")
            and ctx.delivery_wake_needs_correction
        ),
        _apply_delivery_wake,
    ),
    _SettlePreRule(
        "entity_or_cover_zero_tool",
        lambda ctx: (
            (ctx.st.entity_routed or _utterance_cover_hit(ctx.st))
            and ctx.st.tg is not None
            and ctx.st.tg.call_to_self
            and not ctx.st.tool_call_list
            and ctx.non_capability_interactive
            and ctx.non_silence_msg
        ),
        _apply_entity_or_cover,
    ),
    _SettlePreRule(
        "master_title",
        lambda ctx: (
            ctx.non_capability_interactive
            and ctx.non_silence_msg
            and title_mentioned(forbid_title_for(ctx.st, ctx.host.persona_name), ctx.match_msg)
        ),
        _apply_master_title,
    ),
    _SettlePreRule(
        "voice_block",
        lambda ctx: ctx.non_capability_interactive and bool(_voice_block_reason(ctx.st, ctx.match_msg)),
        _apply_voice_block,
        post_fab_allowed=True,
    ),
    _SettlePreRule(
        "render_obligation",
        lambda ctx: (
            ctx.render_obligation
            and not ctx.st.delegated_render
            and not ctx.st.pending_async_delivery
            and not ctx.st.image_sent_this_run
            and not bool(_RENDER_TOOL_NAMES & set(ctx.st.tool_call_list))
            and ctx.non_capability_interactive
        ),
        _apply_render_obligation,
        post_fab_allowed=True,
    ),
)


async def run_settle_policy_ring(host: SettleRingHost, inp: SettleRingInput) -> SettleRingOutcome:
    """执行回合结束策略环：前置结构纠偏（最多一次）→ 出口话术裁决 → 必要时单次重说。"""
    st = inp.st
    # 发送工具已直接向当前用户交付，loop 层已拦截尾部复述；禁止再对弃文触发纠偏、二次出图或 INV-4 补发。
    if (
        host.create_by in _MAIN_PERSONA_CREATE_BY
        and st.return_mode != "return"
        and "direct_user_sent" in st.run_extra
        and st.run_extra["direct_user_sent"] is True
    ):
        if st.fab_blocked:
            host.scrub_fake_done_history({t.strip() for t in st.fab_blocked})
        rs_direct = inp.result_msg.strip()
        if rs_direct and not is_silence_marker(rs_direct) and rs_direct not in host.sent_texts:
            if not st.silence_reason:
                st.silence_reason = "already_sent_by_tool"
            return SettleRingOutcome(result_msg="<SILENCE>", replacement_visible=True)
        return SettleRingOutcome(result_msg=inp.result_msg, replacement_visible=True)

    render_obligation = needs_render_obligation(st, inp.match_msg, allow_render=inp.allow_render)
    post_fab_only = bool(st.fab_blocked and st.effectual_mutate and st.bot and st.return_mode in ("always", "by_bot"))
    if post_fab_only:
        logger.info(i18n_t("log.agent.fakedone_claim"))
        await inp.resend_fab_blocked()

    sctx = _SettlePreState(
        host=host,
        st=st,
        result_msg=inp.result_msg,
        match_msg=inp.match_msg,
        allow_render=inp.allow_render,
        render_obligation=render_obligation,
        resend_fab_blocked=inp.resend_fab_blocked,
    )
    outcome = SettleRingOutcome(result_msg=inp.result_msg)
    correction_ran = False
    for rule in _SETTLE_PRE_RING:
        if post_fab_only and not rule.post_fab_allowed:
            continue
        if rule.match(sctx):
            outcome = await rule.apply(sctx)
            correction_ran = True
            break

    if host.create_by not in ("Chat", "Agent") or not outcome.result_msg or st.return_mode == "return":
        return outcome

    gate_reason = ""
    gate_body = ""
    rs = outcome.result_msg.strip()
    if rs not in host.sent_texts:
        exit_ctx = SpeechContext(
            policy="free",
            pending_async=st.pending_async_delivery,
            image_sent=st.image_sent_this_run,
            has_status_tool=st.has_status_tool_call,
            tool_calls_so_far=st.tool_call_list,
            wait_comfort_sent=st.wait_comfort_sent,
            fact_pack_pending=st.saw_structured_return,
            has_active_task=st.has_active_task,
            render_inflight=st.delegated_render,
            user_text=turn_user_text(st),
            allow_render=inp.allow_render,
            in_settle_exit=True,
            render_obligation=render_obligation,
            skip_report_exit=outcome.skip_report_exit,
            is_delivery_wake=sctx.delivery_wake,
        )
        blocked, why = evaluate_speech_ring(exit_ctx, rs)
        if blocked:
            gate_reason = why
            gate_body = rs
            outcome.result_msg = "<SILENCE>"

    if (
        gate_reason
        and gate_body
        and not correction_ran
        and not st.fake_done_retry
        and host.create_by in _INTERACTIVE_CREATE_BY
        and host.create_by != "CapabilityAgent"
    ):
        rewrite = rewrite_directive_for(
            gate_reason,
            allow_render=inp.allow_render,
            tool_calls=len(st.tool_call_list),
            recited_report=looks_like_report_speech(gate_body),
            forbid_title=forbid_title_for(st, host.persona_name),
            wait_already_sent=st.wait_comfort_sent,
            image_sent=st.image_sent_this_run,
        )
        if rewrite is not None:
            disputes_before = host.dispute_count
            rewritten = await host.try_correction_pass(
                st,
                (rewrite,),
                suppress_intermediate_text=True,
                return_mode="return",
            )
            disputed = host.dispute_count > disputes_before
            if disputed:
                logger.info(i18n_t("log.agent.directive_disputed", reason=host.latest_dispute_preview()))
            next_msg = _voice_retry_text(rewritten, disputed=disputed, blocked=gate_body, user_text=turn_user_text(st))
            if not is_silence_marker(next_msg):
                outcome.result_msg = next_msg
                await host.emit_unsent_reply(st, next_msg)
                gate_reason = ""
                outcome.replacement_visible = True

    if gate_reason:
        st.silence_reason = gate_reason
        host.note_output_block(st, gate_reason, gate_body)
    return outcome
