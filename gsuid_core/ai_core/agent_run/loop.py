"""阶段 C：Agent.iter 环（ModelRequest / CallTools / End）"""

from __future__ import annotations

import time
from typing import Any, Sequence

from pydantic_graph import End
from pydantic_ai.agent import CallToolsNode, ModelRequestNode
from pydantic_ai.messages import (
    TextPart,
    ModelMessage,
    ThinkingPart,
    ToolCallPart,
    TextPartDelta,
    PartDeltaEvent,
    PartStartEvent,
    ToolReturnPart,
    UserPromptPart,
    ThinkingPartDelta,
    NativeToolCallPart,
    NativeToolReturnPart,
    ModelResponseStreamEvent,
)
from pydantic_ai.exceptions import UsageLimitExceeded

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core import wall_clock, output_gate
from gsuid_core.ai_core.delivery import send_chat_result
from gsuid_core.ai_core.thinking import ThinkTagSplitter
from gsuid_core.ai_core.control.lines import (
    ITER_WRAP_NUDGE,
    WALL_CLOCK_WRAP_NUDGE,
    TOKEN_BUDGET_WRAP_NUDGE,
)
from gsuid_core.ai_core.protocol_text import is_silence_marker
from gsuid_core.ai_core.agent_run.host import RunOnceHost
from gsuid_core.ai_core.request_effort import EffortCapture, effort_capture, take_request_effort
from gsuid_core.ai_core.agent_run.state import (
    RunOnceState,
    _require_limits,
    _require_context,
)
from gsuid_core.ai_core.agent_run.guards import (
    DeliveryGuard,
    ToolCircuitBreaker,
)
from gsuid_core.ai_core.agent_run.support import (
    _MAIN_PERSONA_CREATE_BY,
    iter_wrap_due,
    add_response_usage,
    canonical_tool_call,
    record_output_block,
    turn_reply_metadata,
    wall_clock_budget_action,
    absorb_run_image_delivered,
    fold_stale_capability_tool_returns,
)
from gsuid_core.ai_core.agent_run.opening_line import (
    arm_opening_line,
    disarm_opening_line,
    turn_expects_opening,
)
from gsuid_core.ai_core.agent_run.speech_policy import (
    SpeechContext,
    non_master_title,
    is_status_tool_name,
    evaluate_speech_policy,
    normalize_speech_policy,
)


def _response_has_function_tool_call(parts: Sequence[object]) -> bool:
    """同响应是否含函数工具（不含 hosted NativeToolCall）。"""
    return any(isinstance(p, ToolCallPart) and not isinstance(p, NativeToolCallPart) for p in parts)


def send_message_call_has_visible_text(parts: Sequence[object]) -> bool:
    """同一次回复里已有带台词的 send_message_by_ai：那句就是开口，不再代发开场句。"""
    for p in parts:
        if not isinstance(p, ToolCallPart) or p.tool_name != "send_message_by_ai":
            continue
        args = p.args_as_dict()
        raw = args["text"] if "text" in args else ""
        if isinstance(raw, str) and raw.strip() and not is_silence_marker(raw.strip()):
            return True
    return False


class LoopPhase:
    async def _run_once_on_model_request(
        self: RunOnceHost,
        st: RunOnceState,
        node: Any,
        agent_run: Any,
    ) -> None:
        """ModelRequestNode：墙钟/闸门/thrash 注入 + ToolReturn 折叠 + 流式请求。"""
        logger.debug(i18n_t("log.agent.trigger_node_modelrequestnode"))

        _inject_iter_wrap_if_due(self, st, node)
        _enforce_wall_clock_budget(self, st, node)
        # §1.7 显式豁免：仅 CapabilityAgent 单次长跑、且旧段可折叠总量超 120k 才成批折叠
        if self.create_by == "CapabilityAgent":
            fold_stale_capability_tool_returns(agent_run.ctx.state.message_history)
        DeliveryGuard.track_post_final_request(st)

        from gsuid_core.ai_core.mcp.pii import apply_context_mask

        is_internal_cap = (
            self.is_subagent and bool(self.capability_node_id) and self.capability_node_id != "render_agent"
        )
        node.request.parts = apply_context_mask(
            node.request.parts,
            is_group=False if is_internal_cap else (st.ev is None or bool(st.ev.group_id)),
        )

        DeliveryGuard.inject_request_contracts(st, node.request)
        ToolCircuitBreaker.maybe_inject_thrash_fuse(st, node.request, create_by=self.create_by)

        ret_summary = await ToolCircuitBreaker.process_tool_returns(
            st,
            node.request,
            create_by=self.create_by,
            session_id=self.session_id or "",
            persona_name=self.persona_name,
            session_logger=self._session_logger,
            emit_trace=self._emit_trace,
        )
        DeliveryGuard.absorb_opening_line(st, session_logger=self._session_logger)
        ToolCircuitBreaker.inject_post_return_nudges(
            st,
            node.request,
            ret_summary,
            create_by=self.create_by,
            session_id=self.session_id or "",
            capability_node_id=self.capability_node_id,
        )

        logger.debug(i18n_t("log.agent.sending_request_waiting_think_send"))
        # 工具回执先落盘，再标下一轮对话。否则瀑布把回执画进后一轮。
        self._session_logger.log_node_transition("ModelRequestNode")
        st.req_start = time.perf_counter()
        st.first_event_at = None
        st.last_event_at = None
        st.thinking_streamed = False
        st.stream_saw_fn_tool = False
        bot = st.bot if st.outbound_stream else None
        if bot is not None:
            bot.reset_text_stream()
        text_starts: set[int] = set()
        tags = st.thinking_tags if st.thinking_tags else ("<think>", "</think>")
        splitter = ThinkTagSplitter(tags[0], tags[1])
        with effort_capture() as effort_slot:
            try:
                async with node.stream(agent_run.ctx) as request_stream:
                    _log_captured_thinking_effort(self, effort_slot)
                    try:
                        async for event in request_stream:
                            if st.cancel_ev is not None and st.cancel_ev.is_set():
                                st.generation_cancelled = True
                                logger.info(i18n_t("log.agent.generation_cancelled_supersede"))
                                break
                            st.last_event_at = time.perf_counter()
                            if st.first_event_at is None:
                                st.first_event_at = st.last_event_at
                            await _handle_stream_event(self, st, event, text_starts=text_starts, splitter=splitter)
                    finally:
                        vis, th = splitter.flush()
                        if th:
                            st.thinking_streamed = True
                            self._emit_trace("thinking_delta", th)
                        if vis:
                            await _push_stream_text_delta(self, st, vis, None)
                        if bot is not None:
                            await bot.flush_text_delta()
            finally:
                _log_captured_thinking_effort(self, effort_slot)

    async def _run_once_on_call_tools(
        self: RunOnceHost,
        st: RunOnceState,
        node: Any,
        statistics_manager: Any,
    ) -> None:
        """CallToolsNode：响应清洗、工具/文本分发、pre_send_gate、性能打点。"""
        logger.debug(i18n_t("log.agent.trigger_node_calltoolsnode"))
        self._session_logger.log_node_transition("CallToolsNode")

        ToolCircuitBreaker.sanitize_and_cap_response(st, node.model_response, create_by=self.create_by)
        DeliveryGuard.prescan_render_delegation(st, node.model_response.parts)

        saw_tool_call_this_turn = _response_has_function_tool_call(node.model_response.parts)
        resp_unsent: list[str] = []
        output_gate.begin_response_batch(_require_context(st).extra)
        ab_attempt_counted = False
        resp_tool_calls: list[str] = []
        from gsuid_core.ai_core.tool_safety import WIRE_TOOLS_SNAPSHOTS_KEY

        wire_extra = _require_context(st).extra
        if WIRE_TOOLS_SNAPSHOTS_KEY in wire_extra and isinstance(wire_extra[WIRE_TOOLS_SNAPSHOTS_KEY], list):
            for wsnap in wire_extra[WIRE_TOOLS_SNAPSHOTS_KEY]:
                if isinstance(wsnap, list):
                    self._session_logger.log_wire_tools_list([str(x) for x in wsnap])
            wire_extra[WIRE_TOOLS_SNAPSHOTS_KEY].clear()

        def _discard(preview: str = "") -> None:
            bot = st.bot if st.outbound_stream else None
            if bot is not None:
                bot.discard_streamed_preview(preview)

        async def _try_send(txt: str, at_uid: str | None) -> bool:
            return await _try_send_gated(self, st, txt, at_user_id=at_uid)

        for part in node.model_response.parts:
            if isinstance(part, ToolCallPart):
                saw_tool_call_this_turn = True
                _dispatch_tool_call_part(self, st, part, resp_tool_calls)
            elif isinstance(part, (NativeToolCallPart, NativeToolReturnPart)):
                if isinstance(part, NativeToolCallPart):
                    st.tool_call_list.append(part.tool_name)
                    self._session_logger.log_tool_call(part.tool_name, part.args, part.tool_call_id)
                    self._emit_trace("tool", f"{part.tool_name}|hosted")
            elif isinstance(part, TextPart):
                text = DeliveryGuard.filter_text_pre_gate(
                    st,
                    part.content,
                    run_sent_texts=self._run_sent_texts,
                    session_logger=self._session_logger,
                    discard_preview=_discard,
                )
                if text is None:
                    continue
                if (
                    "direct_user_sent" in st.run_extra
                    and st.run_extra["direct_user_sent"] is True
                    and self.create_by in _MAIN_PERSONA_CREATE_BY
                    and st.return_mode != "return"
                ):
                    st.last_block_reason = "already_sent_by_tool"
                    record_output_block(st, self._session_logger, "already_sent_by_tool", text)
                    _discard(text)
                    continue
                if st.suppress_intermediate_text and saw_tool_call_this_turn:
                    logger.debug(i18n_t("log.agent.suppressing_intermediate_text", p0=repr(text[:40])))
                    resp_unsent.append(text)
                    _discard(text)
                    continue

                hard = 0
                if self.create_by in _MAIN_PERSONA_CREATE_BY and self.persona_name:
                    from gsuid_core.ai_core.persona.chat_style import resolve_chat_style

                    hard = resolve_chat_style(self.persona_name).hard
                st.saw_final_response = True
                speech_ctx: SpeechContext | None = None
                if self.create_by in _MAIN_PERSONA_CREATE_BY and st.return_mode != "return":
                    speech_ctx = _build_speech_context(self, st, hard=hard)
                    if not st.bot:
                        blk, why = evaluate_speech_policy(speech_ctx, text)
                        if blk:
                            DeliveryGuard.record_speech_block(
                                st,
                                why,
                                text,
                                session_logger=self._session_logger,
                                discard_preview=_discard,
                            )
                            continue
                ab_attempt_counted = await DeliveryGuard.process_gated_text_part(
                    st,
                    text,
                    speech_ctx=speech_ctx,
                    ab_attempt_counted=ab_attempt_counted,
                    session_logger=self._session_logger,
                    run_sent_texts=self._run_sent_texts,
                    run_sent_order=self._run_sent_order,
                    resp_unsent=resp_unsent,
                    discard_preview=_discard,
                    try_send_gated=_try_send,
                )
            elif isinstance(part, ThinkingPart):
                thinking = part.content.strip()
                logger.debug(i18n_t("log.agent.llm_thinking", _thinking=thinking))
                if thinking:
                    st.thinking_segments.append(thinking)
                self._session_logger.log_thinking(thinking)
                if thinking and not st.thinking_streamed:
                    self._emit_trace("thinking", thinking)

        opening_extra = _require_context(st).extra
        disarm_opening_line(opening_extra)
        if (
            saw_tool_call_this_turn
            and st.main_channel_sends == 0
            and not st.wait_comfort_sent
            and not send_message_call_has_visible_text(node.model_response.parts)
            and turn_expects_opening(
                create_by=self.create_by,
                is_subagent=self.is_subagent,
                is_framework=st.fw_msg,
                is_status_inquiry=st.status_inquiry,
            )
        ):
            arm_opening_line(
                opening_extra,
                text=resp_unsent[0] if resp_unsent else "",
                deliver=st.bot is not None and st.return_mode in ("always", "by_bot"),
            )

        if resp_unsent:
            st.unsent_texts.extend(resp_unsent)
            unsent_left = list(resp_unsent)
            kept_parts = []
            for p in node.model_response.parts:
                if unsent_left and isinstance(p, TextPart) and p.content.strip() == unsent_left[0]:
                    unsent_left.pop(0)
                    continue
                kept_parts.append(p)
            node.model_response.parts = kept_parts

        ToolCircuitBreaker.record_response_thrash(st, resp_tool_calls)
        _record_call_tools_metrics(self, st, node, statistics_manager)

    async def _run_once_iter_and_settle(
        self: RunOnceHost,
        st: RunOnceState,
        _agent: Any,
        statistics_manager: Any,
    ) -> Any:
        """Agent.iter 环；成功后交给 ``_run_once_settle_result``。"""
        logger.info(i18n_t("log.agent.iter_start"))
        logger.info(i18n_t("log.agent.current_history", p0=len(self.history)))

        final_user = st.final_user_message
        assert final_user is not None
        self._history_iter_active = True
        try:
            async with _agent.iter(
                final_user,
                deps=_require_context(st),
                message_history=self.history,
                usage_limits=_require_limits(st),
            ) as agent_run:
                async for node in agent_run:
                    if st.cancel_ev is not None and st.cancel_ev.is_set():
                        st.generation_cancelled = True
                        logger.info(i18n_t("log.agent.generation_cancelled_supersede"))
                        break
                    if isinstance(node, ModelRequestNode):
                        if _model_request_is_approval_halt(node):
                            agent_run.ctx.state.message_history.append(node.request)
                            st.approval_halted = True
                            logger.info(i18n_t("log.ai.approval_halt_stop"))
                            break
                        await self._run_once_on_model_request(st, node, agent_run)
                        if st.generation_cancelled:
                            break
                    elif isinstance(node, CallToolsNode):
                        await self._run_once_on_call_tools(st, node, statistics_manager)
                    elif isinstance(node, End):
                        logger.debug(i18n_t("log.agent.node_trigger_end"))
                        logger.debug(i18n_t("log.agent.run_ended_final_result_generated"))
                        self._session_logger.log_node_transition("End")

            if st.generation_cancelled:
                logger.info(i18n_t("log.agent.generation_aborted_no_history"))
                return "" if st.output_type is None else None

            if st.approval_halted:
                _commit_approval_halt_history(self.history, agent_run.all_messages())
                return "" if st.output_type is None else None

            return await self._run_once_settle_result(st, agent_run, statistics_manager)
        finally:
            self._history_iter_active = False


async def _push_stream_text_delta(
    host: RunOnceHost,
    st: RunOnceState,
    piece: str,
    splitter: ThinkTagSplitter | None,
) -> None:
    if not piece or st.bot is None:
        return
    visible = piece
    if splitter is not None:
        visible, thought = splitter.feed(piece)
        if thought:
            st.thinking_streamed = True
            host._emit_trace("thinking_delta", thought)
    if not visible or st.speech_policy == "silence_only":
        return
    if st.suppress_intermediate_text and st.stream_saw_fn_tool:
        return
    bot = st.bot if st.outbound_stream else None
    if bot is not None:
        bot.enqueue_text_delta(visible)


async def _handle_stream_event(
    host: RunOnceHost,
    st: RunOnceState,
    event: ModelResponseStreamEvent,
    *,
    text_starts: set[int],
    splitter: ThinkTagSplitter,
) -> None:
    if isinstance(event, PartDeltaEvent):
        delta = event.delta
        if isinstance(delta, ThinkingPartDelta):
            piece = delta.content_delta or ""
            if piece:
                st.thinking_streamed = True
                host._emit_trace("thinking_delta", piece)
        elif isinstance(delta, TextPartDelta):
            if delta.content_delta:
                await _push_stream_text_delta(host, st, delta.content_delta, splitter)
        return
    if isinstance(event, PartStartEvent):
        part = event.part
        if isinstance(part, ThinkingPart):
            piece = part.content or ""
            if piece:
                st.thinking_streamed = True
                host._emit_trace("thinking_delta", piece)
        elif isinstance(part, TextPart):
            if event.index in text_starts and event.previous_part_kind is None:
                return
            text_starts.add(event.index)
            if part.content:
                await _push_stream_text_delta(host, st, part.content, splitter)
        elif isinstance(part, ToolCallPart) and not isinstance(part, NativeToolCallPart):
            st.stream_saw_fn_tool = True


async def _commit_streamed_text(
    host: RunOnceHost,
    st: RunOnceState,
    text: str,
    *,
    already_streamed: bool,
    at_user_id: str | None,
) -> None:
    bot = st.bot
    if bot is None:
        return
    if already_streamed:
        if st.outbound_stream:
            await bot.commit_streamed_history(text, extra_metadata=turn_reply_metadata(st.ev))
        host._run_sent_texts.add(text)
        if text not in host._run_sent_order:
            host._run_sent_order.append(text)
        st.main_channel_sends += 1
        return
    mention_raw = st.run_extra["mention_names"] if "mention_names" in st.run_extra else None
    mentions: dict[str, str] = {}
    if isinstance(mention_raw, dict):
        for mk, mv in mention_raw.items():
            if isinstance(mk, str) and isinstance(mv, str) and mk and mv:
                mentions[mk] = mv
    await send_chat_result(
        bot,
        text,
        ev=st.ev,
        at_user_id=at_user_id,
        mention_names=mentions,
        extra_metadata=turn_reply_metadata(st.ev),
    )
    absorb_run_image_delivered(st)
    host._run_sent_texts.add(text)
    if text not in host._run_sent_order:
        host._run_sent_order.append(text)
    st.main_channel_sends += 1


async def _try_send_gated(host: RunOnceHost, st: RunOnceState, text: str, *, at_user_id: str | None) -> bool:
    try:
        already = False
        bot = st.bot if st.outbound_stream else None
        if bot is not None:
            leftover = bot.take_unsent_suffix(text)
            if leftover is None:
                already = True
            elif leftover != text:
                bot.enqueue_text_delta(leftover)
                await bot.flush_text_delta()
                bot.reset_text_stream()
                already = True
        await _commit_streamed_text(host, st, text, already_streamed=already, at_user_id=at_user_id)
    except Exception as _e:
        logger.debug(i18n_t("log.agent.text_send_fail_failed", _e=_e))
        return False
    return True


def _inject_iter_wrap_if_due(host: RunOnceHost, st: RunOnceState, node: Any) -> None:
    st.model_requests += 1
    limit = st.limits.request_limit if st.limits is not None else None
    if not iter_wrap_due(
        requests_started=st.model_requests,
        request_limit=limit,
        return_mode=st.return_mode,
        create_by=host.create_by,
        already_nudged=st.iter_nudged,
    ):
        return
    if limit is None:
        return
    node.request.parts = [*node.request.parts, UserPromptPart(content=ITER_WRAP_NUDGE)]
    st.iter_nudged = True
    logger.info(i18n_t("log.agent.iter_wrap_nudge", p0=st.model_requests, p1=limit))


def _enforce_wall_clock_budget(host: RunOnceHost, st: RunOnceState, node: Any) -> None:
    elapsed = time.time() - st.start_time - wall_clock.excluded_seconds(st.wall_acc)
    action = wall_clock_budget_action(
        wall_clock_budget=host.wall_clock_budget,
        elapsed_sec=elapsed,
        requests_started=st.model_requests,
        nudge_request=st.wall_nudge_request,
        create_by=host.create_by,
        input_tokens=st.usage_input_tokens,
        output_tokens=st.usage_output_tokens,
    )
    if action == "nudge":
        node.request.parts = [*node.request.parts, UserPromptPart(content=WALL_CLOCK_WRAP_NUDGE)]
        st.wall_nudge_request = st.model_requests
        logger.info(i18n_t("log.agent.wall_clock_soft_budget", p0=elapsed))
    elif action == "token_nudge":
        node.request.parts = [*node.request.parts, UserPromptPart(content=TOKEN_BUDGET_WRAP_NUDGE)]
        st.wall_nudge_request = st.model_requests
        logger.info(
            i18n_t(
                "log.agent.token_budget_soft_cap",
                p0=st.usage_input_tokens,
                p1=st.usage_output_tokens,
            )
        )
    elif action == "hard_stop":
        st.wall_timed_out = True
        raise UsageLimitExceeded("wall_clock_budget_exceeded")
    elif action == "token_hard_stop":
        st.token_budget_exceeded = True
        raise UsageLimitExceeded("token_budget_exceeded")


def _log_captured_thinking_effort(host: RunOnceHost, slot: EffortCapture) -> None:
    seen, effort = take_request_effort(slot)
    if not seen:
        return
    host._session_logger.log_request_thinking_effort(effort)


def _dispatch_tool_call_part(
    host: RunOnceHost,
    st: RunOnceState,
    part: ToolCallPart,
    resp_tool_calls: list[str],
) -> None:
    logger.debug(i18n_t("log.agent.llm_requests_tool_name_args", p0=part.tool_name, p1=part.args))
    call_eff_name = part.tool_name
    call_eff_args: object = part.args
    if part.tool_name == "call_tool":
        call_args_dict = part.args_as_dict()
        if "tool_name" in call_args_dict and isinstance(call_args_dict["tool_name"], str):
            inner_tn = call_args_dict["tool_name"].strip()
            if inner_tn:
                call_eff_name = inner_tn
        if "arguments_json" in call_args_dict:
            call_eff_args = call_args_dict["arguments_json"]
    if part.tool_call_id:
        st.effective_tool_names[part.tool_call_id] = call_eff_name
    st.tool_call_list.append(call_eff_name)
    resp_tool_calls.append(canonical_tool_call(part.tool_name, part.args))
    if call_eff_name == "create_subagent":
        from gsuid_core.ai_core.agent_run.support import _tool_call_targets_render_agent

        if _tool_call_targets_render_agent(part):
            st.delegated_render = True
            st.pending_async_delivery = True
            st.speech_policy = "silence_only"
    if is_status_tool_name(call_eff_name):
        st.has_status_tool_call = True
        _require_context(st).extra["has_status_tool"] = True
    host._session_logger.log_tool_call(part.tool_name, part.args, part.tool_call_id)
    host._emit_trace("tool", f"{call_eff_name}|{part.args_as_json_str()}")

    try:
        from gsuid_core.ai_core.memory.config import memory_config as mem_cfg

        if mem_cfg.enable_preference_memory and st.ev is not None:
            from gsuid_core.ai_core.memory.ingestion.tool_trace import record_tool_call

            record_tool_call(str(st.ev.user_id), call_eff_name, call_eff_args)
    except Exception:
        pass


def _build_speech_context(host: RunOnceHost, st: RunOnceState, *, hard: int) -> SpeechContext:
    from gsuid_core.ai_core.agent_node.registry import persona_allows_capability_agent

    fact_pending = bool(st.saw_structured_return and not st.delegated_render and not st.image_sent_this_run)
    gate_extra = _require_context(st).extra
    at_raw = gate_extra["at_user_id"] if "at_user_id" in gate_extra else None
    addr = str(at_raw) if isinstance(at_raw, str) and at_raw else ""
    if not addr and st.ev is not None and st.ev.user_id:
        addr = str(st.ev.user_id)
    allow_render = (
        not ("delivery_failed" in st.run_extra and st.run_extra["delivery_failed"] is True)
    ) and persona_allows_capability_agent(host.persona_name, "render_agent")
    user_q = (
        st.ev.raw_text
        if st.ev is not None and st.ev.raw_text
        else (st.last_user_question or (st.user_message if isinstance(st.user_message, str) else ""))
    )
    return SpeechContext(
        policy=normalize_speech_policy(st.speech_policy),
        pending_async=st.pending_async_delivery,
        image_sent=st.image_sent_this_run,
        has_status_tool=st.has_status_tool_call,
        tool_calls_so_far=st.tool_call_list,
        wait_comfort_sent=st.wait_comfort_sent,
        fact_pack_pending=fact_pending,
        has_active_task=st.has_active_task,
        render_inflight=bool(st.delegated_render and not st.image_sent_this_run),
        speech_len_hard=hard,
        user_asked_detail=False,
        forbid_title=non_master_title(addr, host.persona_name),
        entity_routed=st.entity_routed,
        user_text=user_q,
        allow_render=allow_render,
    )


def _record_call_tools_metrics(
    host: RunOnceHost,
    st: RunOnceState,
    node: Any,
    statistics_manager: Any,
) -> None:
    ttft_ms = 0.0
    tps = 0.0
    req_usage = node.model_response.usage
    resp_model = node.model_response.model_name
    add_response_usage(
        st,
        input_tokens=req_usage.input_tokens,
        output_tokens=req_usage.output_tokens,
        cache_read_tokens=req_usage.cache_read_tokens,
        cache_write_tokens=req_usage.cache_write_tokens,
        response_model=resp_model if isinstance(resp_model, str) else None,
    )
    if st.first_event_at is not None and st.last_event_at is not None:
        ttft_ms = round((st.first_event_at - st.req_start) * 1000, 2)
        generation_time = st.last_event_at - st.first_event_at
        if req_usage.output_tokens > 0 and generation_time > 0:
            tps = round(req_usage.output_tokens / generation_time, 2)
        logger.debug(i18n_t("log.ai_agent.ttft_ms_tps_tokens_ok", ttft_ms=ttft_ms, tps=tps))
    req_tool_calls = sum(1 for p in node.model_response.parts if isinstance(p, ToolCallPart))
    statistics_manager.record_hourly_performance(
        provider=st.provider,
        model_name=st.model_name,
        ttft_ms=ttft_ms,
        tps=tps,
        input_tokens=req_usage.input_tokens,
        output_tokens=req_usage.output_tokens,
        cache_read_tokens=req_usage.cache_read_tokens,
        cache_write_tokens=req_usage.cache_write_tokens,
        tool_call_count=req_tool_calls,
    )
    host._session_logger.log_step_usage(
        input_tokens=req_usage.input_tokens,
        output_tokens=req_usage.output_tokens,
        cache_read_tokens=req_usage.cache_read_tokens,
        cache_write_tokens=req_usage.cache_write_tokens,
        model_name=resp_model if isinstance(resp_model, str) else st.model_name,
        ttft_ms=ttft_ms,
        tps=tps,
        tool_calls=req_tool_calls,
    )
    st.first_event_at = None
    st.last_event_at = None


def _model_request_is_approval_halt(node: ModelRequestNode[Any]) -> bool:
    from gsuid_core.ai_core.approval.halt import text_is_approval_halt

    for part in node.request.parts:
        if type(part) is not ToolReturnPart:
            continue
        content = part.content if isinstance(part.content, str) else ""
        if text_is_approval_halt(content):
            return True
    return False


def _commit_approval_halt_history(history: list[ModelMessage], messages: list[ModelMessage]) -> None:
    """把已完成的工具往返写进 history，避免下一次 run 留下没有回执的工具调用。"""
    if messages is history:
        return
    start = len(history)
    if len(messages) > start:
        history.extend(messages[start:])
