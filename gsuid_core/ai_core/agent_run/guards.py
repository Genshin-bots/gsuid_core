"""Run-once 环内协作者：DeliveryGuard（投递/话术小状态机）与 ToolCircuitBreaker（空转/失败熔断器）。"""

from __future__ import annotations

from typing import Callable, Sequence, Awaitable
from dataclasses import dataclass

from pydantic_ai.messages import (
    TextPart,
    ModelMessage,
    ModelRequest,
    ToolCallPart,
    ModelResponse,
    ToolReturnPart,
    UserPromptPart,
)

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core import output_gate
from gsuid_core.ai_core.control import angle_guard as angle_bracket_guard
from gsuid_core.ai_core.delivery import send_chat_result
from gsuid_core.ai_core.thinking import _split_embedded_thinking
from gsuid_core.ai_core.control.lines import TOOL_FAIL_FUSE_NUDGE
from gsuid_core.ai_core.protocol_text import is_silence_marker, remainder_after_protocol_tags
from gsuid_core.ai_core.session_logger import AISessionLogger
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.agent_run.state import RunOnceState, _require_context
from gsuid_core.ai_core.history_compact import (
    _canonicalize_tool_call_args_in_parts,
    _sanitize_tool_call_artifacts_in_parts,
    normalize_escaped_control_tags_in_parts,
)
from gsuid_core.ai_core.agent_run.support import (
    _THRASH_FUSE_NUDGE,
    _INTERACTIVE_CREATE_BY,
    _MAIN_PERSONA_CREATE_BY,
    TraceKind,
    thrash_limit_for,
    _claims_fake_done,
    canonical_tool_call,
    record_output_block,
    turn_reply_metadata,
    record_run_tool_output,
    _tool_return_looks_failed,
    tool_return_is_hard_error,
    absorb_run_image_delivered,
    _tool_return_is_async_pending,
    _tool_call_targets_render_agent,
    _tool_return_is_effectual_write,
    _update_thrash_streak_for_response,
)
from gsuid_core.ai_core.control.directive import DISPUTE_CLOSED_KEY, GateDecision
from gsuid_core.ai_core.agent_run.opening_line import take_opening_result
from gsuid_core.ai_core.agent_run.speech_policy import (
    ZERO_OUTPUT_VOICE_REASONS,
    MAIN_CHANNEL_VISIBLE_LIMIT,
    SpeechContext,
    content_is_render_candidate,
)
from gsuid_core.ai_core.control.outbound_detectors import TECH_DUMP_TOOL_SHIELD, is_tech_dump
from gsuid_core.ai_core.capability_agents.delegation_contracts import (
    DELEGATION_INFLIGHT_KEY as _DELEGATION_INFLIGHT_KEY,
    PENDING_DELEGATION_HOLD as _PENDING_DELEGATION_HOLD,
    POST_TOOL_FAIL_CONTRACT as _POST_TOOL_FAIL_CONTRACT,
    POST_TOOL_OUTPUT_CONTRACT as _POST_TOOL_OUTPUT_CONTRACT,
    PENDING_DELEGATION_OPENING as _PENDING_DELEGATION_OPENING,
    POST_DISPUTE_SILENCE_CONTRACT as _POST_DISPUTE_SILENCE_CONTRACT,
    POST_TOOL_FAIL_CONTRACT_RENDER as _POST_TOOL_FAIL_CONTRACT_RENDER,
    POST_TOOL_OUTPUT_CONTRACT_RENDER as _POST_TOOL_OUTPUT_CONTRACT_RENDER,
    POST_TOOL_FAIL_CONTRACT_CAPABILITY as _POST_TOOL_FAIL_CONTRACT_CAPABILITY,
    POST_TOOL_OUTPUT_CONTRACT_CAPABILITY as _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
    POST_TOOL_OUTPUT_CONTRACT_RENDER_NO_EMBED as _POST_TOOL_OUTPUT_CONTRACT_RENDER_NO_EMBED,
    post_tool_contracts_for as _post_tool_contracts_for,
    web_search_delegate_line as _web_search_delegate_line,
    receipt_has_rendered_image as _receipt_has_rendered_image,
    tool_return_is_delegation_refusal as _tool_return_is_delegation_refusal,
    inflight_after_create_subagent_return as _inflight_after_create_subagent_return,
)

_MAX_TOOL_CALLS_PER_RESPONSE = 30
# 能力代理交付/落盘通道永不被单工具失败熔断封杀，避免收集完数据却无法 artifact_put。
_CAPABILITY_DELIVERY_TOOLS: frozenset[str] = frozenset(
    {
        "artifact_put",
        "artifact_get",
        "artifact_list",
        "record_put",
        "record_get",
        "record_list",
        "record_append",
        "record_update",
        "record_summary",
    }
)


@dataclass(frozen=True)
class ToolReturnSummary:
    """单次 ModelRequest 中所有 ToolReturnPart 处理完后的汇总信号。"""

    has_tool_return: bool
    tripped_fail_tool: str
    tripped_fail_streak: int
    tripped_tools: tuple[str, ...] = ()


class DeliveryGuard:
    """投递与话术策略小状态机：集中管理出站静默、委派接管、闸门重写与配额拦截。"""

    @staticmethod
    def track_post_final_request(st: RunOnceState) -> None:
        if st.saw_final_response:
            st.post_final_requests += 1
            if st.post_final_requests > 1:
                st.ab_pending_nudges = []

    @staticmethod
    def inject_request_contracts(st: RunOnceState, request: ModelRequest) -> None:
        """将上一轮 REWRITE feedback、熔断警告与争端闭环静默契约注入当轮 request user 侧。"""
        if st.ab_pending_nudges:
            had_ooc = any(output_gate.is_ooc_judge_feedback(n) for n in st.ab_pending_nudges)
            nudge_body = output_gate.merge_rewrite_feedbacks(st.ab_pending_nudges)
            request.parts = [*request.parts, UserPromptPart(content=nudge_body)]
            logger.warning(i18n_t("log.ai.output_gate_injected_rewrite_feedback"))
            if had_ooc:
                output_gate.mark_ooc_reminded(_require_context(st).extra)
            st.ab_pending_nudges = []

        extra = _require_context(st).extra
        if (output_gate.is_fused(extra) or st.ab_abort) and not output_gate.fuse_already_injected(extra):
            st.ab_abort = True
            output_gate.mark_fuse_injected(extra)
            request.parts = [*request.parts, UserPromptPart(content=angle_bracket_guard.build_fuse_warning())]

        if DISPUTE_CLOSED_KEY in extra and extra[DISPUTE_CLOSED_KEY]:
            st.speech_policy = "silence_only"
            if not any(
                isinstance(p, UserPromptPart) and p.content == _POST_DISPUTE_SILENCE_CONTRACT for p in request.parts
            ):
                request.parts = [*request.parts, UserPromptPart(content=_POST_DISPUTE_SILENCE_CONTRACT)]

    @staticmethod
    def apply_send_message_return(st: RunOnceState) -> None:
        """仅在 send_message_by_ai 真正发出图片后置位，拒发不得跳过 settle 补发。"""
        extra = _require_context(st).extra
        if "image_sent_this_run" not in extra or extra["image_sent_this_run"] is not True:
            return
        st.image_sent_this_run = True
        st.pending_async_delivery = False
        if st.speech_policy == "silence_only":
            st.speech_policy = "framework_deliver" if st.fw_msg else "free"

    @staticmethod
    def apply_create_subagent_return(st: RunOnceState, part: ToolReturnPart, body: str) -> None:
        """create_subagent 回执状态转移：ack 确认在途，失败且未 ack 回滚抢先静默。"""
        async_ack = bool(_tool_return_is_async_pending(part) or ("后台执行" in body) or ("自动回灌" in body))
        refused = _tool_return_is_delegation_refusal(body)
        render_done = _receipt_has_rendered_image(body)
        pending, delegated, policy, ack = _inflight_after_create_subagent_return(
            failed=_tool_return_looks_failed(part),
            refused=refused,
            async_ack=async_ack,
            render_done=render_done,
            ack_seen=st.render_ack_seen,
            pending_async=st.pending_async_delivery,
            delegated_render=st.delegated_render,
            speech_policy=st.speech_policy,
            is_framework=st.fw_msg,
        )
        st.pending_async_delivery = pending
        st.delegated_render = delegated
        st.speech_policy = policy
        st.render_ack_seen = ack
        if async_ack:
            _require_context(st).extra[_DELEGATION_INFLIGHT_KEY] = True
        if (
            not _tool_return_looks_failed(part)
            and not refused
            and not async_ack
            and not render_done
            and content_is_render_candidate(tool_name="create_subagent", content=body, fileos_folded=False)
        ):
            st.saw_structured_return = True

    @staticmethod
    def absorb_opening_line(
        st: RunOnceState,
        *,
        session_logger: AISessionLogger,
        run_history: Sequence[ModelMessage] | None = None,
    ) -> str:
        """工具代发的开场句记进本轮出站账；未发出则标 opening_missed 供后续补一句。"""
        result = take_opening_result(_require_context(st).extra)
        if result is None:
            return ""
        if not result.text:
            if result.source == "create_subagent":
                st.opening_missed = True
            return ""
        restored = ""
        if result.delivered:
            st.main_channel_sends += 1
            delivered_text = result.text.strip()
            for idx, item in enumerate(st.unsent_texts):
                if item.strip() == delivered_text:
                    st.unsent_texts.pop(idx)
                    break
            # soft_latency_ack 发出时仅往尚未送出过的尾响应补回 TextPart，既保 §1.7 又不丢历史上下文
            if result.source == "soft_latency_ack":
                if run_history:
                    tail = run_history[-1]
                    if isinstance(tail, ModelResponse) and not any(
                        isinstance(p, TextPart) and p.content.strip() == delivered_text for p in tail.parts
                    ):
                        tail.parts = [TextPart(content=result.text), *tail.parts]
                else:
                    restored = result.text
        st.wait_comfort_sent = True
        st.ooc_blocked.clear()
        session_logger.log_task_ack(result.text, source=result.source)
        return restored

    @staticmethod
    def prescan_render_delegation(st: RunOnceState, parts: Sequence[object]) -> None:
        """同响应 TextPart 可能排在 ToolCall 前：先扫出图委派，避免念包抢跑。"""
        for part in parts:
            if isinstance(part, ToolCallPart) and part.tool_name == "create_subagent":
                if _tool_call_targets_render_agent(part):
                    st.delegated_render = True
                    st.pending_async_delivery = True
                    st.speech_policy = "silence_only"
                    break

    @staticmethod
    def record_speech_block(
        st: RunOnceState,
        why: str,
        text: str,
        *,
        session_logger: AISessionLogger,
        discard_preview: Callable[[str], None],
    ) -> None:
        """记录话术策略拦截状态并丢弃同轮流式预览。"""
        st.last_block_reason = why
        record_output_block(st, session_logger, why, text)
        # 只记排版失配；不得回写 saw_structured_return（由真实 ToolReturn 置位）。
        if why in ("report_speech", "empty_handoff", "persona_length_breach"):
            if text not in st.presentation_withheld:
                st.presentation_withheld.append(text)
                st.presentation_withheld_reasons.append(why)
            if st.context is not None:
                st.context.extra["dispute_has_prior_reply"] = True
        elif why == "numeric_recitation" or why in ZERO_OUTPUT_VOICE_REASONS:
            st.presentation_withheld_reasons.append(why)
            if st.context is not None:
                st.context.extra["dispute_has_prior_reply"] = True
        logger.info(
            i18n_t(
                "log.agent.silent_skipping_text",
                _text=f"[speech_policy={st.speech_policy}/{why}] {text[:60]}",
            )
        )
        discard_preview(text)

    @staticmethod
    async def process_gated_text_part(
        st: RunOnceState,
        text: str,
        *,
        speech_ctx: SpeechContext | None,
        ab_attempt_counted: bool,
        session_logger: AISessionLogger,
        run_sent_texts: set[str],
        run_sent_order: list[str],
        resp_unsent: list[str],
        discard_preview: Callable[[str], None],
        try_send_gated: Callable[[str, str | None], Awaitable[bool]],
    ) -> bool:
        """运行 pre_send_gate / 假完成 / 单轮出站上限并下发可见文本；返回更新后的 attempt 计数标识。"""
        if st.bot and text and st.return_mode in ["always", "by_bot"]:
            user_raw = st.ev.raw_text if st.ev is not None and st.ev.raw_text else ""
            gr = output_gate.pre_send_gate(
                text,
                _require_context(st).extra,
                user_text=user_raw,
                channel="main",
                count_attempt=not ab_attempt_counted,
                is_group=bool(st.ev is not None and st.ev.group_id),
                speech_ctx=speech_ctx,
            )
            if gr.decision is GateDecision.FUSE:
                st.ab_abort = True
                st.last_block_reason = "output_gate_fuse"
                record_output_block(st, session_logger, "output_gate_fuse", text)
                logger.warning(
                    i18n_t(
                        "log.ai.output_gate_drop_text_after_fuse",
                        policy=gr.policy,
                        preview=repr(text[:80]),
                    )
                )
                discard_preview(text)
                return True
            if gr.decision is GateDecision.REWRITE:
                if gr.policy == "speech_policy":
                    DeliveryGuard.record_speech_block(
                        st,
                        gr.detail or "speech_policy",
                        text,
                        session_logger=session_logger,
                        discard_preview=discard_preview,
                    )
                    return ab_attempt_counted
                gate_why = (
                    f"output_gate_{gr.policy}"
                    if gr.policy in ("angle_bracket", "ooc", "inner_os")
                    else "output_gate_rewrite"
                )
                st.last_block_reason = gate_why
                record_output_block(st, session_logger, gate_why, text)
                next_counted = ab_attempt_counted
                if gr.policy == "ooc" and gr.ooc_hit is not None:
                    logger.warning(
                        i18n_t(
                            "log.agent.firewall_main_output_hit_ooc",
                            p0=gr.ooc_hit.category,
                            p1=gr.ooc_hit.matched,
                        )
                    )
                    if gr.defer_ooc:
                        st.ooc_blocked.append((text, gr.ooc_hit))
                    if gr.feedback:
                        st.ab_pending_nudges.append(gr.feedback)
                else:
                    if gr.policy == "angle_bracket":
                        next_counted = True
                    if gr.feedback:
                        st.ab_pending_nudges.append(gr.feedback)
                    if gr.fused:
                        st.ab_abort = True
                discard_preview(text)
                return next_counted
            if gr.decision is GateDecision.FALLBACK:
                if gr.send_text.strip() != text.strip():
                    st.last_block_reason = "output_gate_fallback"
                    record_output_block(st, session_logger, "output_gate_fallback", text)
                discard_preview(text)
                fb = gr.send_text
                fb_sent = False
                if fb:
                    try:
                        await send_chat_result(
                            st.bot,
                            fb,
                            ev=st.ev,
                            ooc_check=False,
                            extra_metadata=turn_reply_metadata(st.ev),
                        )
                        absorb_run_image_delivered(st)
                        run_sent_texts.add(fb)
                        if fb not in run_sent_order:
                            run_sent_order.append(fb)
                        st.main_channel_sends += 1
                        fb_sent = True
                    except Exception as me:
                        logger.debug(i18n_t("log.agent.text_send_fail_failed", _e=me))
                if fb_sent:
                    st.wait_comfort_sent = True
                return ab_attempt_counted
            if gr.decision is GateDecision.ALLOW and gr.send_text:
                text = gr.send_text

            fab_gate_on = not st.fake_done_retry and not st.effectual_mutate and bool(st.tool_names)
            if fab_gate_on and _claims_fake_done(text):
                logger.warning(i18n_t("log.agent.fakedone_zero_claim_pending_ok", p0=repr(text[:40])))
                st.last_block_reason = "fake_done_withheld"
                record_output_block(st, session_logger, "fake_done_withheld", text)
                st.fab_blocked.append(text)
                discard_preview(text)
                return ab_attempt_counted

            if st.main_channel_sends >= MAIN_CHANNEL_VISIBLE_LIMIT:
                st.last_block_reason = "main_channel_cap"
                record_output_block(st, session_logger, "main_channel_cap", text)
                logger.info(
                    i18n_t(
                        "log.agent.silent_skipping_text",
                        _text=f"[main_channel_cap] {text[:40]}",
                    )
                )
                resp_unsent.append(text)
                discard_preview(text)
                return ab_attempt_counted

            extra = _require_context(st).extra
            at_uid = extra["at_user_id"] if "at_user_id" in extra else None
            at = str(at_uid) if isinstance(at_uid, str) and at_uid else None
            if await try_send_gated(text, at):
                st.ooc_blocked.clear()
                st.wait_comfort_sent = True
        elif text and st.return_mode == "return" and not st.fake_done_retry:
            run_sent_texts.add(text)
            if text not in run_sent_order:
                run_sent_order.append(text)
        return ab_attempt_counted

    @staticmethod
    def filter_text_pre_gate(
        st: RunOnceState,
        raw_content: str,
        *,
        run_sent_texts: set[str],
        session_logger: AISessionLogger,
        discard_preview: Callable[[str], None],
    ) -> str | None:
        """处理空白/SILENCE/协议标签与历史去重；返回应进入下一级的文本或 None。"""
        text = raw_content.strip()
        if not text:
            discard_preview("")
            return None
        logger.debug(i18n_t("log.agent.llm_text", _text=text))
        session_logger.log_text_output(text)
        if is_silence_marker(text):
            if not st.silence_reason:
                st.silence_reason = "model_voluntary"
            if output_gate.ooc_reminder_delivered(_require_context(st).extra):
                st.ooc_blocked.clear()
            logger.info(i18n_t("log.agent.silent_skipping_text", _text=text))
            discard_preview(text)
            return None
        stripped_protocol = remainder_after_protocol_tags(text).strip()
        if stripped_protocol != text:
            text = stripped_protocol
            if not text:
                discard_preview("")
                return None
        if text in run_sent_texts:
            logger.debug(i18n_t("log.agent.skipping_duplicate", p0=repr(text[:40])))
            discard_preview(text)
            return None
        return text


class ToolCircuitBreaker:
    """工具空转/连败熔断与工具回执清洗/落盘折叠器。"""

    @staticmethod
    def maybe_inject_thrash_fuse(st: RunOnceState, request: ModelRequest, *, create_by: str) -> None:
        thrash_limit = thrash_limit_for(st.same_tool_name)
        if (
            not st.thrash_fused
            and st.same_tool_name
            and st.same_tool_streak > thrash_limit
            and create_by in _INTERACTIVE_CREATE_BY
        ):
            request.parts = [*request.parts, UserPromptPart(content=_THRASH_FUSE_NUDGE)]
            st.thrash_fused = True
            logger.warning(
                i18n_t(
                    "log.agent.tool_thrash_fuse",
                    tool_name=st.same_tool_name,
                    streak=st.same_tool_streak,
                )
            )

    @staticmethod
    def sanitize_and_cap_response(st: RunOnceState, response: ModelResponse, *, create_by: str) -> None:
        """归一化控制标签、拆分嵌入思考、剥离伪调用、规范参数、截断批量并剥空转重复。"""
        response.parts = normalize_escaped_control_tags_in_parts(response.parts)
        response.parts = _split_embedded_thinking(response.parts, st.thinking_tags)
        response.parts = _sanitize_tool_call_artifacts_in_parts(response.parts)
        response.parts = _canonicalize_tool_call_args_in_parts(response.parts)

        tc_count = sum(1 for p in response.parts if isinstance(p, ToolCallPart))
        if tc_count > _MAX_TOOL_CALLS_PER_RESPONSE:
            logger.warning(
                i18n_t(
                    "log.agent.tool_calls_per_response_truncate",
                    count=tc_count,
                    limit=_MAX_TOOL_CALLS_PER_RESPONSE,
                )
            )
            kept = []
            tc_kept = 0
            for part in response.parts:
                if isinstance(part, ToolCallPart):
                    tc_kept += 1
                    if tc_kept > _MAX_TOOL_CALLS_PER_RESPONSE:
                        continue
                kept.append(part)
            response.parts = kept

        if st.thrash_fused and st.same_tool_name and create_by in _INTERACTIVE_CREATE_BY:
            fused_call = st.same_tool_name
            stripped = [
                part
                for part in response.parts
                if not (isinstance(part, ToolCallPart) and canonical_tool_call(part.tool_name, part.args) == fused_call)
            ]
            if len(stripped) < len(response.parts):
                logger.warning(i18n_t("log.agent.tool_thrash_strip_duplicate", tool_name=st.same_tool_name))
                response.parts = stripped

    @staticmethod
    def record_response_thrash(st: RunOnceState, resp_tool_calls: Sequence[str]) -> None:
        st.same_tool_name, st.same_tool_streak = _update_thrash_streak_for_response(
            list(resp_tool_calls),
            prev_signature=st.same_tool_name,
            prev_streak=st.same_tool_streak,
        )

    @staticmethod
    async def process_tool_returns(
        st: RunOnceState,
        request: ModelRequest,
        *,
        create_by: str,
        session_id: str,
        persona_name: str | None,
        session_logger: AISessionLogger,
        emit_trace: Callable[[TraceKind, str], None],
    ) -> ToolReturnSummary:
        """处理当轮 ToolReturnPart：失败计数、RM 注册、FileOS 落盘折叠、回执状态转移。"""
        has_tool_return = False
        tripped_fail_tool = ""
        tripped_fail_streak = 0
        tripped_tools: list[str] = []
        batch_failed_tools: set[str] = set()

        for part in request.parts:
            if not isinstance(part, ToolReturnPart):
                continue
            has_tool_return = True
            eff_name = (
                st.effective_tool_names[part.tool_call_id]
                if part.tool_call_id and part.tool_call_id in st.effective_tool_names
                else (part.tool_name or "")
            )
            if eff_name == "create_subagent" and _tool_return_is_async_pending(part):
                st.pending_async_delivery = True
            elif not _tool_return_is_async_pending(part):
                if tool_return_is_hard_error(part):
                    nxt = (st.tool_fail_streaks[eff_name] if eff_name in st.tool_fail_streaks else 0) + 1
                    st.tool_fail_streaks[eff_name] = nxt
                    # 同批并行调同一故障工具只记 1 次总失败，避免单源断连 1 轮触发全盘熔断
                    if not eff_name or eff_name not in batch_failed_tools:
                        st.total_tool_failures += 1
                        if eff_name:
                            batch_failed_tools.add(eff_name)
                    if eff_name:
                        st.failed_tools.add(eff_name)
                    if nxt >= 4:
                        if eff_name and eff_name not in tripped_tools:
                            tripped_tools.append(eff_name)
                        if nxt > tripped_fail_streak:
                            tripped_fail_tool = eff_name
                            tripped_fail_streak = nxt
                elif eff_name:
                    st.tool_fail_streaks[eff_name] = 0
                    st.succeeded_tools.add(eff_name)

            if (isinstance(part.content, str) and part.content.startswith("base64://")) or isinstance(
                part.content, bytes
            ):
                resource_id = RM.register(part.content)
                logger.info(
                    i18n_t(
                        "log.agent.content_registered_resource_id",
                        p0=eff_name,
                        resource_id=resource_id,
                    )
                )
                if type(part) is ToolReturnPart:
                    part.content = (
                        f"[工具 {eff_name} 已生成内容，未发送给用户，资源ID: {resource_id}；"
                        "如需发送请调用 send_message_by_ai，严禁把资源ID写入回复正文]"
                    )

            fileos_folded = False
            raw_tr = part.content if isinstance(part.content, str) else None
            record_run_tool_output(st.run_tool_outputs, eff_name, raw_tr)
            if type(part) is ToolReturnPart and raw_tr is not None:
                from gsuid_core.ai_core.planning.runtime import get_plan_context
                from gsuid_core.ai_core.planning.tool_output_helper import (
                    is_searchish_tool,
                    persist_tool_return,
                    persist_and_fold_tool_return,
                    schedule_persist_tool_return,
                )

                pc = get_plan_context()
                tid = (pc.task_id if pc else "") or ""
                rid = (pc.root_task_id if pc else "") or ""
                can_read_handle = (
                    create_by in _MAIN_PERSONA_CREATE_BY
                    or "read_handle" in st.tool_names
                    or (st.context is not None and "read_handle" in st.context.dynamic_tool_names)
                )
                if can_read_handle:
                    is_group = bool(st.ev is not None and st.ev.group_id)
                    try:
                        folded = await persist_and_fold_tool_return(
                            tool_name=eff_name,
                            content=raw_tr,
                            ev=st.ev,
                            session_id=session_id,
                            task_id=tid,
                            root_task_id=rid,
                            is_group=is_group,
                            create_by=create_by,
                        )
                    except Exception as fileos_e:
                        logger.debug(i18n_t("log.ai.tool_output_fold_skip", e=fileos_e))
                        folded = None
                    if folded is not None:
                        part.content = folded
                        fileos_folded = True
                        if create_by in _MAIN_PERSONA_CREATE_BY and content_is_render_candidate(
                            tool_name=eff_name,
                            content=raw_tr,
                            fileos_folded=True,
                        ):
                            st.saw_structured_return = True
                elif is_searchish_tool(eff_name):
                    try:
                        await persist_tool_return(
                            tool_name=eff_name,
                            content=raw_tr,
                            ev=st.ev,
                            session_id=session_id,
                            task_id=tid,
                            root_task_id=rid,
                        )
                    except Exception as fileos_e:
                        logger.debug(i18n_t("log.ai.tool_output_persist_skip", e=fileos_e))
                else:
                    schedule_persist_tool_return(
                        tool_name=eff_name,
                        content=raw_tr,
                        ev=st.ev,
                        session_id=session_id,
                        task_id=tid,
                        root_task_id=rid,
                    )

            if (
                not fileos_folded
                and create_by in _MAIN_PERSONA_CREATE_BY
                and type(part) is ToolReturnPart
                and isinstance(part.content, str)
            ):
                if is_tech_dump(part.content):
                    part.content = TECH_DUMP_TOOL_SHIELD
                else:
                    from gsuid_core.ai_core.history_compact import (
                        _summarize_structured_data,
                        _looks_like_structured_data,
                    )

                    if _looks_like_structured_data(part.content):
                        if content_is_render_candidate(tool_name=eff_name, content=part.content, fileos_folded=False):
                            st.saw_structured_return = True
                        from gsuid_core.ai_core.agent_node.registry import persona_allows_capability_agent

                        fold_suffix = "\n（结构数据已折叠。综合分析请 create_subagent；聊天通道禁止念节点名。）"
                        if persona_allows_capability_agent(persona_name, "render_agent"):
                            fold_suffix = (
                                "\n（结构数据已折叠。综合分析请 create_subagent；"
                                "多项数据 create_subagent(render_agent) 出图，勿台词复述。"
                                "聊天通道禁止念节点名。）"
                            )
                        part.content = _summarize_structured_data(part.content) + fold_suffix
                    elif content_is_render_candidate(tool_name=eff_name, content=part.content, fileos_folded=False):
                        st.saw_structured_return = True
                    if eff_name == "create_subagent":
                        DeliveryGuard.apply_create_subagent_return(st, part, part.content)
                    if eff_name == "send_message_by_ai":
                        DeliveryGuard.apply_send_message_return(st)
            elif type(part) is ToolReturnPart and eff_name == "create_subagent":
                body_raw = (
                    raw_tr if isinstance(raw_tr, str) else (part.content if isinstance(part.content, str) else "")
                )
                DeliveryGuard.apply_create_subagent_return(st, part, body_raw)
            elif type(part) is ToolReturnPart and eff_name == "send_message_by_ai":
                DeliveryGuard.apply_send_message_return(st)

            tool_result_str = str(part.content)
            if len(tool_result_str) > 200:
                tool_result_str = tool_result_str[:200] + f"...[截断, 共{len(tool_result_str)}字符]"
            logger.debug(
                i18n_t(
                    "log.agent.tool_execution_compl_name_result_passed_ok",
                    p0=eff_name,
                    tool_result_str=tool_result_str,
                )
            )
            session_logger.log_tool_return(part.tool_name, part.content, part.tool_call_id)
            trace_out = part.content if isinstance(part.content, str) else str(part.content or "")
            if trace_out.startswith("base64://") or isinstance(part.content, bytes):
                trace_out = "[binary]"
            if trace_out.strip():
                emit_trace("tool_result", f"{eff_name}|{trace_out[:2000]}")
            ret_body = part.content if isinstance(part.content, str) else str(part.content or "")
            if _tool_return_is_effectual_write(eff_name, ret_body, failed=_tool_return_looks_failed(part)):
                st.effectual_mutate = True

        return ToolReturnSummary(
            has_tool_return=has_tool_return,
            tripped_fail_tool=tripped_fail_tool,
            tripped_fail_streak=tripped_fail_streak,
            tripped_tools=tuple(tripped_tools),
        )

    @staticmethod
    def inject_post_return_nudges(
        st: RunOnceState,
        request: ModelRequest,
        summary: ToolReturnSummary,
        *,
        create_by: str,
        session_id: str,
        capability_node_id: str,
    ) -> None:
        """在 ToolReturn 处理完毕后按熔断/异步委派/后置契约/PTC 顺序注入当轮 user 侧提示。"""
        cap_exhausted = create_by == "CapabilityAgent" and st.total_tool_failures >= 8
        if (summary.tripped_fail_streak >= 4 or cap_exhausted) and create_by in _INTERACTIVE_CREATE_BY:
            if not st.fail_fuse_nudged:
                request.parts = [*request.parts, UserPromptPart(content=TOOL_FAIL_FUSE_NUDGE)]
                st.fail_fuse_nudged = True
                logger.warning(
                    i18n_t(
                        "log.agent.tool_thrash_fuse",
                        tool_name=summary.tripped_fail_tool or "capability_batch",
                        streak=summary.tripped_fail_streak or st.total_tool_failures,
                    )
                )
            if create_by == "CapabilityAgent":
                # 仅按故障工具名精确熔断，绝不误用交互聊天的全盘门禁封杀 artifact_put 等交付通道
                blocked = _require_context(st).blocked_tool_names
                for tname in summary.tripped_tools:
                    if tname and tname not in _CAPABILITY_DELIVERY_TOOLS:
                        blocked.add(tname)
                if cap_exhausted:
                    active_failing = [
                        tname
                        for tname in st.failed_tools
                        if tname in st.tool_fail_streaks and st.tool_fail_streaks[tname] > 0
                    ]
                    for tname in active_failing or list(st.failed_tools):
                        if tname and tname not in _CAPABILITY_DELIVERY_TOOLS:
                            blocked.add(tname)

        if summary.has_tool_return and create_by in _INTERACTIVE_CREATE_BY:
            any_fail = False
            any_actionable = False
            for part in request.parts:
                if type(part) is not ToolReturnPart:
                    continue
                if _tool_return_is_async_pending(part):
                    continue
                any_actionable = True
                if _tool_return_looks_failed(part):
                    any_fail = True
            if st.pending_async_delivery:
                any_actionable = False
                hold = _PENDING_DELEGATION_HOLD
                if st.opening_missed and st.main_channel_sends == 0 and not st.wait_comfort_sent:
                    hold = _PENDING_DELEGATION_OPENING
                if not any(
                    isinstance(p, UserPromptPart)
                    and p.content in (_PENDING_DELEGATION_HOLD, _PENDING_DELEGATION_OPENING)
                    for p in request.parts
                ):
                    request.parts = [*request.parts, UserPromptPart(content=hold)]
            if any_actionable:
                ok_c, fail_c = _post_tool_contracts_for(
                    create_by,
                    session_id=session_id,
                    capability_node_id=capability_node_id,
                )
                contract = fail_c if any_fail else ok_c
                from gsuid_core.ai_core.interaction_scaffold import extract_message_body

                delegate_question = (
                    st.ev.raw_text.strip()
                    if st.ev is not None and isinstance(st.ev.raw_text, str) and st.ev.raw_text.strip()
                    else extract_message_body(st.last_user_question)
                )
                delegate = _web_search_delegate_line(
                    create_by=create_by,
                    capability_node_id=capability_node_id,
                    question=delegate_question,
                    first_tool=st.tool_call_list[0] if st.tool_call_list else "",
                    already_nudged=st.web_search_delegate_nudged,
                    any_fail=any_fail,
                )
                if delegate:
                    st.web_search_delegate_nudged = True
                    contract = delegate
                if not any(
                    isinstance(p, UserPromptPart)
                    and p.content
                    in (
                        _POST_TOOL_OUTPUT_CONTRACT,
                        _POST_TOOL_FAIL_CONTRACT,
                        _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
                        _POST_TOOL_FAIL_CONTRACT_CAPABILITY,
                        _POST_TOOL_OUTPUT_CONTRACT_RENDER,
                        _POST_TOOL_OUTPUT_CONTRACT_RENDER_NO_EMBED,
                        _POST_TOOL_FAIL_CONTRACT_RENDER,
                    )
                    for p in request.parts
                ):
                    request.parts = [*request.parts, UserPromptPart(content=contract)]

        if summary.has_tool_return and "run_code" in st.tool_names:
            from gsuid_core.ai_core.register import find_tool_base
            from gsuid_core.ai_core.control.lines import PTC_BATCH_NUDGE

            direct_code_callable = 0
            for part in request.parts:
                if type(part) is not ToolReturnPart:
                    continue
                tname = (
                    st.effective_tool_names[part.tool_call_id]
                    if part.tool_call_id and part.tool_call_id in st.effective_tool_names
                    else (part.tool_name or "")
                )
                if not tname or tname == "run_code":
                    continue
                tb = find_tool_base(tname)
                if tb is not None and tb.code_callable:
                    direct_code_callable += 1
            if direct_code_callable >= 3 and not any(
                isinstance(p, UserPromptPart) and p.content == PTC_BATCH_NUDGE for p in request.parts
            ):
                request.parts = [*request.parts, UserPromptPart(content=PTC_BATCH_NUDGE)]
