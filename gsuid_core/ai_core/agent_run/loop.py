"""阶段 C：Agent.iter 环（ModelRequest / CallTools / End）"""

from __future__ import annotations

import time
from typing import Any, Sequence

from pydantic_graph import End
from pydantic_ai.agent import CallToolsNode, ModelRequestNode
from pydantic_ai.messages import (
    TextPart,
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

from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core import (
    wall_clock,
    output_gate,
    output_firewall,
    angle_bracket_guard,
)
from gsuid_core.ai_core.utils import (
    ThinkTagSplitter,
    send_chat_result,
    is_silence_marker,
    _split_embedded_thinking,
    remainder_after_protocol_tags,
    _canonicalize_tool_call_args_in_parts,
    _sanitize_tool_call_artifacts_in_parts,
)
from gsuid_core.ai_core.agent_run.host import RunOnceHost
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.agent_run.state import (
    RunOnceState,
    _require_limits,
    _require_context,
)
from gsuid_core.ai_core.agent_run.support import (
    _THRASH_FUSE_NUDGE,
    _INTERACTIVE_CREATE_BY,
    _MAIN_PERSONA_CREATE_BY,
    thrash_limit_for,
    _claims_fake_done,
    _wall_clock_nudge_for,
    _tool_return_looks_failed,
    _tool_return_is_async_pending,
    _tool_call_targets_render_agent,
    _tool_return_is_effectual_write,
    _update_thrash_streak_for_response,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.control.directive import DISPUTE_CLOSED_KEY
from gsuid_core.ai_core.agent_run.speech_policy import (
    MAIN_CHANNEL_VISIBLE_LIMIT,
    is_status_tool_name,
    looks_like_wait_comfort,
    strip_open_solicitations,
    content_is_render_candidate,
    looks_like_task_accept_speech,
    should_block_user_visible_text,
)
from gsuid_core.ai_core.agent_run.remote_web_search import is_hosted_web_search_name
from gsuid_core.ai_core.capability_agents.delegation_contracts import (
    POST_TOOL_FAIL_CONTRACT as _POST_TOOL_FAIL_CONTRACT,
    RENDER_DONE_RECEIPT_MARK as _RENDER_DONE_RECEIPT_MARK,
    POST_TOOL_OUTPUT_CONTRACT as _POST_TOOL_OUTPUT_CONTRACT,
    POST_DISPUTE_SILENCE_CONTRACT as _POST_DISPUTE_SILENCE_CONTRACT,
    POST_DELIVERY_SILENCE_CONTRACT as _POST_DELIVERY_SILENCE_CONTRACT,
    POST_TOOL_FAIL_CONTRACT_RENDER as _POST_TOOL_FAIL_CONTRACT_RENDER,
    POST_TOOL_OUTPUT_CONTRACT_RENDER as _POST_TOOL_OUTPUT_CONTRACT_RENDER,
    POST_TOOL_FAIL_CONTRACT_CAPABILITY as _POST_TOOL_FAIL_CONTRACT_CAPABILITY,
    POST_TOOL_OUTPUT_CONTRACT_CAPABILITY as _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
    is_timeless_aggregate as _is_timeless_aggregate,
    post_tool_contracts_for as _post_tool_contracts_for,
    tool_return_has_fresh_mark as _tool_return_has_fresh_mark,
    tool_return_is_non_web_data as _tool_return_is_non_web_data,
    tool_return_has_web_source_mark as _tool_return_has_web_source_mark,
    inflight_after_create_subagent_return as _inflight_after_create_subagent_return,
)


def _response_has_function_tool_call(parts: Sequence[object]) -> bool:
    """同响应是否含函数工具（不含 hosted NativeToolCall）。"""
    return any(isinstance(p, ToolCallPart) and not isinstance(p, NativeToolCallPart) for p in parts)


def decide_text_outbound_slot(
    *,
    has_fn_tool: bool,
    tool_bearing_index: int,
    accept_slot_used: bool,
) -> str:
    """按「第几次带函数 ToolCall 的响应」分槽。返回 send_accept / unsent / send_final。"""
    if has_fn_tool:
        if tool_bearing_index == 1 and not accept_slot_used:
            return "send_accept"
        return "unsent"
    return "send_final"


_STAGE_REPLY_INNER: frozenset[str] = frozenset(
    {"ok", "okay", "yes", "no", "好", "嗯", "哦", "喔", "哈", "行", "是", "对"}
)


def is_stage_direction(text: str) -> bool:
    """成对括号包裹、无句子标点 → 舞台指示，不出站。短确认不算。"""
    body = text.strip()
    if len(body) < 2 or (body[0], body[-1]) not in (("（", "）"), ("(", ")")):
        return False
    inner = body[1:-1].strip()
    if not inner or "<" in inner:
        return False
    if any(ch in inner for ch in "。！？!?；;"):
        return False
    return inner.casefold() not in _STAGE_REPLY_INNER


class LoopPhase(RunOnceHost):
    async def _emit_stream_events(
        self,
        st: RunOnceState,
        event: ModelResponseStreamEvent,
        *,
        text_starts: set[int],
        splitter: ThinkTagSplitter,
    ) -> None:
        """从 pydantic-ai 流式 event 抽出 thinking / 可见 text delta。

        thinking 走 ``on_trace``（旁路轨迹）；text 仅 ``outbound_stream`` 时入队。
        非流式出站仍等完整 TextPart + 闸门。Text 里夹的 ``<think>`` 在此剥掉。
        """
        if isinstance(event, PartDeltaEvent):
            delta = event.delta
            if isinstance(delta, ThinkingPartDelta):
                piece = delta.content_delta or ""
                if piece:
                    st.thinking_streamed = True
                    self._emit_trace("thinking_delta", piece)
            elif isinstance(delta, TextPartDelta):
                piece = delta.content_delta
                if piece:
                    await self._enqueue_text_delta(st, piece, splitter)
            return
        if isinstance(event, PartStartEvent):
            part = event.part
            if isinstance(part, ThinkingPart):
                piece = part.content or ""
                if piece:
                    st.thinking_streamed = True
                    self._emit_trace("thinking_delta", piece)
            elif isinstance(part, TextPart):
                if event.index in text_starts and event.previous_part_kind is None:
                    return
                text_starts.add(event.index)
                piece = part.content or ""
                if piece:
                    await self._enqueue_text_delta(st, piece, splitter)
            elif isinstance(part, ToolCallPart) and not isinstance(part, NativeToolCallPart):
                st.stream_saw_fn_tool = True

    async def _enqueue_text_delta(
        self,
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
                self._emit_trace("thinking_delta", thought)
        if not visible:
            return
        # 终局静默 / 已见函数工具的中间 OS 不要打到流式出站
        if st.speech_policy in ("silence_only", "delivered"):
            return
        if st.suppress_intermediate_text and st.stream_saw_fn_tool:
            return
        bot = self._outbound_bot(st)
        if bot is None:
            return
        bot.enqueue_text_delta(visible)

    def _outbound_bot(self, st: RunOnceState) -> Bot | None:
        """出站流式模式才返回 bot；pydantic-ai 流式打点不走这里。"""
        if not st.outbound_stream:
            return None
        return st.bot

    async def _flush_bot_text_delta(self, st: RunOnceState) -> None:
        bot = self._outbound_bot(st)
        if bot is not None:
            await bot.flush_text_delta()

    def _discard_stream_preview(self, st: RunOnceState, text: str = "") -> None:
        bot = self._outbound_bot(st)
        if bot is not None:
            bot.discard_streamed_preview(text)

    async def _commit_streamed_or_send(
        self,
        st: RunOnceState,
        text: str,
        *,
        already_streamed: bool,
        at_user_id: str | None = None,
    ) -> None:
        bot = st.bot
        if bot is None:
            return
        if already_streamed:
            if st.outbound_stream:
                await bot.commit_streamed_history(text)
            self._run_sent_texts.add(text)
            st.main_channel_sends += 1
            return
        await send_chat_result(bot, text, ev=st.ev, at_user_id=at_user_id)
        self._run_sent_texts.add(text)
        st.main_channel_sends += 1

    async def _send_gated_text(self, st: RunOnceState, text: str, *, at_user_id: str | None) -> None:
        """闸门已通过：流式只补未推后缀，否则 ``send_chat_result``。"""
        already = False
        bot = self._outbound_bot(st)
        if bot is not None:
            leftover = bot.take_unsent_suffix(text)
            if leftover is None:
                already = True
            elif leftover != text:
                bot.enqueue_text_delta(leftover)
                await bot.flush_text_delta()
                # leftover 已出站；本段吃掉整段流，对齐缓冲勿留给下一 part
                bot.reset_text_stream()
                already = True
        await self._commit_streamed_or_send(st, text, already_streamed=already, at_user_id=at_user_id)

    def _apply_create_subagent_return(self, st: RunOnceState, part: ToolReturnPart, body: str) -> None:
        """create_subagent 回执：ack 确认在途，失败且未 ack 则回滚抢先静默。"""
        async_ack = bool(_tool_return_is_async_pending(part) or ("后台执行" in body) or ("自动回灌" in body))
        pending, delegated, policy, ack = _inflight_after_create_subagent_return(
            failed=_tool_return_looks_failed(part),
            async_ack=async_ack,
            render_done=_RENDER_DONE_RECEIPT_MARK in body,
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
        if (
            not _tool_return_looks_failed(part)
            and not async_ack
            and _RENDER_DONE_RECEIPT_MARK not in body
            and content_is_render_candidate(
                tool_name="create_subagent",
                content=body,
                fileos_folded=False,
            )
        ):
            st.saw_structured_return = True

    async def _run_once_on_model_request(
        self,
        st: RunOnceState,
        node: Any,
        agent_run: Any,
    ) -> None:
        """ModelRequestNode：墙钟/闸门/ thrash 注入 + ToolReturn 折叠 + 流式请求。"""
        logger.debug(i18n_t("log.agent.trigger_node_modelrequestnode"))

        self._session_logger.log_node_transition("ModelRequestNode")
        if st.saw_final_response:
            st.post_final_requests += 1
            if st.post_final_requests > 1:
                st.ab_pending_nudges = []

        # 先扫本请求内 ToolReturn 形态，再决定墙钟文案（避免事实包刚返回却注入「禁工具」）
        for _pre in node.request.parts:
            if isinstance(_pre, NativeToolReturnPart) and is_hosted_web_search_name(_pre.tool_name):
                st.saw_web_source = True
                continue
            if type(_pre) is not ToolReturnPart:
                continue
            _pb = _pre.content if isinstance(_pre.content, str) else ""
            _ptn = _pre.tool_name or ""
            if _tool_return_is_async_pending(_pre):
                st.pending_async_delivery = True
            if _ptn == "create_subagent" and _RENDER_DONE_RECEIPT_MARK in _pb:
                st.delegated_render = True
            elif content_is_render_candidate(
                tool_name=_ptn,
                content=_pb,
                fileos_folded=False,
            ):
                st.saw_structured_return = True
            # 无时点聚合：只记内部账，不再往请求里塞禁令。
            if _pb and _is_timeless_aggregate(_pb):
                st.saw_timeless_aggregate = True
            # 时效账本：web 滞后 / as_of 新鲜 / 其它成功非 web（挡「只有 web」误报）
            if _pb and _tool_return_has_web_source_mark(_pb):
                st.saw_web_source = True
            if _pb and _tool_return_has_fresh_mark(_pb):
                st.saw_fresh_data = True
            elif (
                _pb
                and not _tool_return_is_async_pending(_pre)
                and not _tool_return_looks_failed(_pre)
                and _tool_return_is_non_web_data(_pb)
            ):
                st.saw_non_web_data = True

        # C-4 墙钟软预算：交互式 run 超时后，请求前注入收敛提示（只注入一次），
        _wall_budget = (
            self.wall_clock_budget
            if self.wall_clock_budget is not None
            else float(ai_config.get_config("scaffold_wall_clock_budget").data)
        )
        _wall_elapsed = time.time() - st.start_time - wall_clock.excluded_seconds(st.wall_acc)
        if (
            not st.wall_nudged
            and _wall_budget > 0
            and self.create_by in _INTERACTIVE_CREATE_BY
            and _wall_elapsed > _wall_budget
        ):
            _need_pipe = bool(st.saw_structured_return and not st.delegated_render)
            _wall_txt = _wall_clock_nudge_for(need_render_pipeline=_need_pipe)
            node.request.parts = [*node.request.parts, UserPromptPart(content=_wall_txt)]
            st.wall_nudged = True
            logger.info(
                i18n_t(
                    "log.agent.wall_clock_soft_budget",
                    p0=_wall_elapsed,
                )
            )

        # 输出闸门：上一轮 REWRITE feedback（多段已合并）注入下一轮请求
        if st.ab_pending_nudges:
            _nudge_body = output_gate.merge_rewrite_feedbacks(st.ab_pending_nudges)
            node.request.parts = [
                *node.request.parts,
                UserPromptPart(content=_nudge_body),
            ]
            logger.warning(i18n_t("log.ai.output_gate_injected_rewrite_feedback"))
            st.ab_pending_nudges = []
        # 熔断提示只注入一次（与 thrash fuse 同形）
        if (output_gate.is_fused(_require_context(st).extra) or st.ab_abort) and not output_gate.fuse_already_injected(
            _require_context(st).extra
        ):
            st.ab_abort = True
            output_gate.mark_fuse_injected(_require_context(st).extra)
            node.request.parts = [
                *node.request.parts,
                UserPromptPart(content=angle_bracket_guard.build_fuse_warning()),
            ]

        _extra_now = _require_context(st).extra
        if DISPUTE_CLOSED_KEY in _extra_now and _extra_now[DISPUTE_CLOSED_KEY]:
            st.speech_policy = "silence_only"
            if not any(
                isinstance(p, UserPromptPart) and p.content == _POST_DISPUTE_SILENCE_CONTRACT
                for p in node.request.parts
            ):
                node.request.parts = [
                    *node.request.parts,
                    UserPromptPart(content=_POST_DISPUTE_SILENCE_CONTRACT),
                ]

        # 同工具空转熔断：连续同名工具 ≥ 阈值后，下一轮模型请求前注入一次收敛提示
        _thrash_limit = thrash_limit_for(st.same_tool_name)
        if not st.thrash_fused and st.same_tool_streak >= _thrash_limit and self.create_by in _INTERACTIVE_CREATE_BY:
            node.request.parts = [*node.request.parts, UserPromptPart(content=_THRASH_FUSE_NUDGE)]
            st.thrash_fused = True
            logger.warning(
                i18n_t(
                    "log.agent.tool_thrash_fuse",
                    tool_name=st.same_tool_name,
                    streak=st.same_tool_streak,
                )
            )

        _has_tool_return = False
        for part in node.request.parts:
            if isinstance(part, ToolReturnPart):
                _has_tool_return = True
                # 如果工具返回b64图片或者bytes内容, 则调用RM实例上传
                if (isinstance(part.content, str) and part.content.startswith("base64://")) or isinstance(
                    part.content, bytes
                ):
                    resource_id = RM.register(part.content)
                    logger.info(
                        i18n_t(
                            "log.agent.content_registered_resource_id",
                            p0=part.tool_name,
                            resource_id=resource_id,
                        )
                    )
                    # v2.0: ToolReturnPart.content 在标注中是 str|Any,
                    # 仅 ToolReturnPart 分支, 其他 part 类型跳过替换。
                    if type(part) is ToolReturnPart:
                        # 工具返回过长时写入短占位，避免污染上下文
                        part.content = f"[工具 {part.tool_name} 已生成内容, 但未发送给用户, 资源ID: {resource_id}]"

                # FileOS：主人格先落盘并折叠长文；能力代理旁路落盘保留全文
                _fileos_folded = False
                _raw_tr = part.content if isinstance(part.content, str) else None
                if type(part) is ToolReturnPart and _raw_tr is not None:
                    from gsuid_core.ai_core.planning.runtime import get_plan_context
                    from gsuid_core.ai_core.planning.tool_output_helper import (
                        is_searchish_tool,
                        persist_tool_return,
                        persist_and_fold_tool_return,
                        schedule_persist_tool_return,
                    )

                    _pc = get_plan_context()
                    _tid = (_pc.task_id if _pc else "") or ""
                    _rid = (_pc.root_task_id if _pc else "") or ""
                    if self.create_by in _MAIN_PERSONA_CREATE_BY:
                        _is_group = bool(st.ev is not None and st.ev.group_id)
                        try:
                            _folded = await persist_and_fold_tool_return(
                                tool_name=part.tool_name or "",
                                content=_raw_tr,
                                ev=st.ev,
                                session_id=self.session_id or "",
                                task_id=_tid,
                                root_task_id=_rid,
                                is_group=_is_group,
                            )
                        except Exception as _fileos_e:
                            logger.debug(i18n_t("log.ai.tool_output_fold_skip", e=_fileos_e))
                            _folded = None
                        if _folded is not None:
                            part.content = _folded
                            _fileos_folded = True
                            # 折叠≠可出图：仅形态达标才武装 render nudge
                            if content_is_render_candidate(
                                tool_name=part.tool_name or "",
                                content=_raw_tr or "",
                                fileos_folded=True,
                            ):
                                st.saw_structured_return = True
                    elif is_searchish_tool(part.tool_name or ""):
                        try:
                            await persist_tool_return(
                                tool_name=part.tool_name or "",
                                content=_raw_tr,
                                ev=st.ev,
                                session_id=self.session_id or "",
                                task_id=_tid,
                                root_task_id=_rid,
                            )
                        except Exception as _fileos_e:
                            logger.debug(i18n_t("log.ai.tool_output_persist_skip", e=_fileos_e))
                    else:
                        schedule_persist_tool_return(
                            tool_name=part.tool_name or "",
                            content=_raw_tr,
                            ev=st.ev,
                            session_id=self.session_id or "",
                            task_id=_tid,
                            root_task_id=_rid,
                        )

                # 仅主人格折叠 JSON（防 OOC）；已 FileOS 折叠则跳过
                if (
                    not _fileos_folded
                    and self.create_by in _MAIN_PERSONA_CREATE_BY
                    and type(part) is ToolReturnPart
                    and isinstance(part.content, str)
                ):
                    if output_firewall.is_tech_dump(part.content):
                        part.content = output_firewall.TECH_DUMP_TOOL_SHIELD
                    else:
                        from gsuid_core.ai_core.utils import (
                            _summarize_structured_data,
                            _looks_like_structured_data,
                        )

                        if _looks_like_structured_data(part.content):
                            if content_is_render_candidate(
                                tool_name=part.tool_name or "",
                                content=part.content,
                                fileos_folded=False,
                            ):
                                st.saw_structured_return = True
                            part.content = (
                                _summarize_structured_data(part.content)
                                + "\n（结构数据已折叠。综合分析请 create_subagent；"
                                "多项数据 create_subagent(render_agent) 出图，勿台词复述。"
                                "聊天通道禁止念节点名。）"
                            )
                        elif content_is_render_candidate(
                            tool_name=part.tool_name or "",
                            content=part.content,
                            fileos_folded=False,
                        ):
                            st.saw_structured_return = True
                        # create_subagent：完成/异步 ack 确认在途；失败回滚抢先静默
                        if (part.tool_name or "") == "create_subagent":
                            self._apply_create_subagent_return(st, part, part.content)
                elif type(part) is ToolReturnPart and (part.tool_name or "") == "create_subagent":
                    _body_raw = (
                        _raw_tr if isinstance(_raw_tr, str) else (part.content if isinstance(part.content, str) else "")
                    )
                    self._apply_create_subagent_return(st, part, _body_raw)

                # 返回的可能是对象也可能是字符串，这里为了打印转成 str
                tool_result_str = str(part.content)
                if len(tool_result_str) > 200:
                    tool_result_str = tool_result_str[:200] + f"...[截断, 共{len(tool_result_str)}字符]"
                logger.debug(
                    i18n_t(
                        "log.agent.tool_execution_compl_name_result_passed_ok",
                        p0=part.tool_name,
                        tool_result_str=tool_result_str,
                    )
                )
                self._session_logger.log_tool_return(part.tool_name, part.content, part.tool_call_id)
                _trace_out = part.content if isinstance(part.content, str) else str(part.content or "")
                if _trace_out.startswith("base64://") or isinstance(part.content, bytes):
                    _trace_out = "[binary]"
                if _trace_out.strip():
                    self._emit_trace("tool_result", f"{part.tool_name or ''}|{_trace_out[:2000]}")
                _ret_body = part.content if isinstance(part.content, str) else str(part.content or "")
                if _tool_return_is_effectual_write(
                    part.tool_name or "",
                    _ret_body,
                    failed=_tool_return_looks_failed(part),
                ):
                    st.effectual_mutate = True

        # 事件驱动输出契约：仅终态工具返回才注入（异步 ack 不触发）
        if _has_tool_return and self.create_by in _INTERACTIVE_CREATE_BY:
            _any_fail = False
            _any_actionable = False
            for _p in node.request.parts:
                if type(_p) is not ToolReturnPart:
                    continue
                if _tool_return_is_async_pending(_p):
                    continue
                _any_actionable = True
                if _tool_return_looks_failed(_p):
                    _any_fail = True
            # 交付终局：send_message_by_ai 已带台词成功交付（工具侧结构信号）。
            # media-only 交付不置位——保留一句角色收尾额度（post_image_ok）。
            _extra_ref = _require_context(st).extra
            if (
                "delivered_with_speech" in _extra_ref
                and bool(_extra_ref["delivered_with_speech"])
                and not st.delivered_terminal
            ):
                st.delivered_terminal = True
                st.speech_policy = "delivered"
            if st.pending_async_delivery:
                _any_actionable = False
            if _any_actionable:
                if st.delivered_terminal:
                    # 交付已完成：不再注入 POST_TOOL 契约（那会提醒模型「再说一句」），
                    # 只注入一次终局 SILENCE 指令（4.3）
                    if not st.delivered_nudged and not any(
                        isinstance(p, UserPromptPart) and p.content == _POST_DELIVERY_SILENCE_CONTRACT
                        for p in node.request.parts
                    ):
                        node.request.parts = [
                            *node.request.parts,
                            UserPromptPart(content=_POST_DELIVERY_SILENCE_CONTRACT),
                        ]
                        st.delivered_nudged = True
                else:
                    _ok_c, _fail_c = _post_tool_contracts_for(
                        self.create_by,
                        session_id=self.session_id or "",
                        capability_node_id=self.capability_node_id,
                    )
                    _contract = _fail_c if _any_fail else _ok_c
                    if (
                        not _any_fail
                        and st.tool_call_list
                        and st.tool_call_list[0] == "web_search_tool"
                        and not st.web_search_delegate_nudged
                    ):
                        from gsuid_core.ai_core.agent_node.registry import match_capability_node

                        _nid = match_capability_node(st.last_user_question)
                        if _nid:
                            st.web_search_delegate_nudged = True
                            _contract = (
                                f"（系统：当前问题已命中能力节点 `{_nid}`，"
                                f'请 create_subagent(agent_profile="{_nid}", task=...) 委派，不要继续网页检索。）'
                            )
                    # 不再把多点结构升级成「唯一合法下一步 = 出图」，也不再叠
                    # 气候/仅 web 禁令。工具返回上的 [source=web] / [as_of=] 够模型自己判断。
                    if not any(
                        isinstance(p, UserPromptPart)
                        and p.content
                        in (
                            _POST_TOOL_OUTPUT_CONTRACT,
                            _POST_TOOL_FAIL_CONTRACT,
                            _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
                            _POST_TOOL_FAIL_CONTRACT_CAPABILITY,
                            _POST_TOOL_OUTPUT_CONTRACT_RENDER,
                            _POST_TOOL_FAIL_CONTRACT_RENDER,
                        )
                        for p in node.request.parts
                    ):
                        node.request.parts = [
                            *node.request.parts,
                            UserPromptPart(content=_contract),
                        ]

        logger.debug(i18n_t("log.agent.sending_request_waiting_think_send"))
        # 以流式方式发起本轮模型请求并逐 event 打点： 普通的节点迭代走非流式请求，
        st.req_start = time.perf_counter()
        st.first_event_at = None
        st.last_event_at = None
        st.thinking_streamed = False
        st.stream_saw_fn_tool = False
        bot = self._outbound_bot(st)
        if bot is not None:
            bot.reset_text_stream()
        _text_starts: set[int] = set()
        _tags = st.thinking_tags if st.thinking_tags else ("<think>", "</think>")
        splitter = ThinkTagSplitter(_tags[0], _tags[1])
        async with node.stream(agent_run.ctx) as request_stream:
            try:
                async for _event in request_stream:
                    if st.cancel_ev is not None and st.cancel_ev.is_set():
                        st.generation_cancelled = True
                        logger.info(i18n_t("log.agent.generation_cancelled_supersede"))
                        break
                    st.last_event_at = time.perf_counter()
                    if st.first_event_at is None:
                        st.first_event_at = st.last_event_at
                    await self._emit_stream_events(st, _event, text_starts=_text_starts, splitter=splitter)
            finally:
                vis, th = splitter.flush()
                if th:
                    st.thinking_streamed = True
                    self._emit_trace("thinking_delta", th)
                if vis:
                    await self._enqueue_text_delta(st, vis, None)
                await self._flush_bot_text_delta(st)

    async def _run_once_on_call_tools(
        self,
        st: RunOnceState,
        node: Any,
        statistics_manager: Any,
    ) -> None:
        """CallToolsNode：响应清洗、工具/文本分发、pre_send_gate、性能打点。"""
        logger.debug(i18n_t("log.agent.trigger_node_calltoolsnode"))

        self._session_logger.log_node_transition("CallToolsNode")

        # 流式请求下 pydantic_ai 未必能拆出内嵌 <think> 标签（仅当标签作为 独立 SSE chunk
        node.model_response.parts = _split_embedded_thinking(node.model_response.parts, st.thinking_tags)
        # 紧接着清除文本里泄漏的工具调用标记残留（弱模型 / 兼容网关常把工具
        # 调用以文本标签输出而非结构化 function calling），整体替换保持三处一致。
        node.model_response.parts = _sanitize_tool_call_artifacts_in_parts(node.model_response.parts)
        # 规范化工具参数（去重复键）：防退化参数串回放时被网关 400（§12.22 事故 #2）
        node.model_response.parts = _canonicalize_tool_call_args_in_parts(node.model_response.parts)

        # 熔断：单次响应工具调用数上限，防弱模型批量幻觉
        _MAX_TOOL_CALLS_PER_RESPONSE = 30
        _tc_count = sum(1 for p in node.model_response.parts if isinstance(p, ToolCallPart))
        if _tc_count > _MAX_TOOL_CALLS_PER_RESPONSE:
            logger.warning(
                i18n_t(
                    "log.agent.tool_calls_per_response_truncate",
                    count=_tc_count,
                    limit=_MAX_TOOL_CALLS_PER_RESPONSE,
                )
            )
            _kept: list = []
            _tc_kept = 0
            for _p in node.model_response.parts:
                if isinstance(_p, ToolCallPart):
                    _tc_kept += 1
                    if _tc_kept > _MAX_TOOL_CALLS_PER_RESPONSE:
                        continue
                _kept.append(_p)
            node.model_response.parts = _kept

        # thrash fuse 后：若仍连打同一工具，直接从本响应剥掉，逼模型换路或收束
        if st.thrash_fused and st.same_tool_name and self.create_by in _INTERACTIVE_CREATE_BY:
            _stripped = [
                _p
                for _p in node.model_response.parts
                if not (isinstance(_p, ToolCallPart) and _p.tool_name == st.same_tool_name)
            ]
            if len(_stripped) < len(node.model_response.parts):
                logger.warning(
                    i18n_t(
                        "log.agent.tool_thrash_strip_duplicate",
                        tool_name=st.same_tool_name,
                    )
                )
                node.model_response.parts = _stripped

        # 同响应 TextPart 可能排在 ToolCall 前面：先扫出图委派，避免念包抢跑。
        for _p in node.model_response.parts:
            if isinstance(_p, ToolCallPart) and _p.tool_name == "create_subagent":
                if _tool_call_targets_render_agent(_p):
                    st.delegated_render = True
                    st.pending_async_delivery = True
                    if st.speech_policy != "delivered":
                        st.speech_policy = "silence_only"
                    break

        # 出站槽：按本 run 第几次「含函数 ToolCall」的响应分。hosted 搜索不当函数工具。
        _saw_tool_call_this_turn = _response_has_function_tool_call(node.model_response.parts)
        if _saw_tool_call_this_turn:
            st.tool_bearing_responses += 1
        _accept_slot_used = False
        _resp_unsent: list[str] = []
        # 同 ModelResponse 多 TextPart：尖括号 attempt 只计 1 次
        output_gate.begin_response_batch(_require_context(st).extra)
        _ab_attempt_counted_this_response = False
        # thrash：同响应内工具名列表，结束本响应后一次性按「轮」更新 streak
        _resp_tool_names: list[str] = []
        for part in node.model_response.parts:
            # 拦截到模型即将调用工具
            if isinstance(part, ToolCallPart):
                _saw_tool_call_this_turn = True
                logger.debug(
                    i18n_t(
                        "log.agent.llm_requests_tool_name_args",
                        p0=part.tool_name,
                        p1=part.args,
                    )
                )
                st.tool_call_list.append(part.tool_name)
                _resp_tool_names.append(part.tool_name)
                if part.tool_name == "create_subagent" and _tool_call_targets_render_agent(part):
                    st.delegated_render = True
                    # 出图委派当下即在途：后续 TextPart 不得把事实包念进群聊。
                    st.pending_async_delivery = True
                    if st.speech_policy != "delivered":
                        st.speech_policy = "silence_only"
                if is_status_tool_name(part.tool_name):
                    st.has_status_tool_call = True
                    _require_context(st).extra["has_status_tool"] = True
                if part.tool_name == "send_message_by_ai":
                    st.image_sent_this_run = True
                    # 发图后解除异步静默，允许一句角色收尾（步骤 7）
                    st.pending_async_delivery = False
                    if st.speech_policy == "silence_only":
                        st.speech_policy = "framework_deliver" if st.fw_msg else "free"
                self._session_logger.log_tool_call(part.tool_name, part.args, part.tool_call_id)
                self._emit_trace("tool", f"{part.tool_name}|{part.args_as_json_str()}")

                # 程序性记忆（默认开；关闭时零影响）：记一笔工具调用轨迹，
                try:
                    from gsuid_core.ai_core.memory.config import memory_config as _mem_cfg

                    if _mem_cfg.enable_preference_memory and st.ev is not None:
                        from gsuid_core.ai_core.memory.ingestion.tool_trace import record_tool_call

                        record_tool_call(str(st.ev.user_id), part.tool_name, part.args)
                except Exception:
                    pass

            # hosted 工具计入 tool_call_list，避免 settle 判零工具。
            # 不置 _saw_tool_call_this_turn，否则 suppress 会丢掉同响应里的最终答案。
            elif isinstance(part, (NativeToolCallPart, NativeToolReturnPart)):
                if is_hosted_web_search_name(part.tool_name):
                    st.saw_web_source = True
                if isinstance(part, NativeToolCallPart):
                    st.tool_call_list.append(part.tool_name)
                    self._session_logger.log_tool_call(part.tool_name, part.args, part.tool_call_id)
                    self._emit_trace("tool", f"{part.tool_name}|hosted")

            # 大模型直接输出文本
            elif isinstance(part, TextPart):
                _text = part.content.strip()
                # 拆出 <think> 后只剩空白的文本片段（如纯思考+工具调用轮），既无需打印也无需下发
                if not _text:
                    self._discard_stream_preview(st)
                    continue
                logger.debug(i18n_t("log.agent.llm_text", _text=_text))
                self._session_logger.log_text_output(_text)
                if is_silence_marker(_text):
                    logger.info(i18n_t("log.agent.silent_skipping_text", _text=_text))
                    self._discard_stream_preview(st, _text)
                    continue
                _stripped_protocol = remainder_after_protocol_tags(_text).strip()
                if _stripped_protocol != _text:
                    _text = _stripped_protocol
                    if not _text:
                        self._discard_stream_preview(st)
                        continue
                if is_stage_direction(_text):
                    _resp_unsent.append(_text)
                    self._discard_stream_preview(st, _text)
                    continue
                if _text in self._run_sent_texts:
                    logger.debug(i18n_t("log.agent.skipping_duplicate", p0=repr(_text[:40])))
                    self._discard_stream_preview(st, _text)
                    continue
                # 同响应已有函数工具：规划/内心 OS 不出站。主人格一句接任务应除外（不按 12 字）。
                # 不按工具名特判。hosted 搜索不置位（答案就在 TextPart）。
                _hard = 0
                if self.create_by in _MAIN_PERSONA_CREATE_BY and self.persona_name:
                    from gsuid_core.ai_core.persona.config import persona_config_manager

                    _pc = persona_config_manager.get_config(self.persona_name)
                    _hard = int(_pc.get_config("speech_len_hard").data)
                _slot = "send_final"
                if st.suppress_intermediate_text:
                    _slot = decide_text_outbound_slot(
                        has_fn_tool=_saw_tool_call_this_turn,
                        tool_bearing_index=st.tool_bearing_responses,
                        accept_slot_used=_accept_slot_used,
                    )
                if _slot == "unsent":
                    logger.debug(i18n_t("log.agent.suppressing_intermediate_text", p0=repr(_text[:40])))
                    _resp_unsent.append(_text)
                    self._discard_stream_preview(st, _text)
                    continue
                if _slot == "send_final":
                    st.saw_final_response = True
                if _slot == "send_accept":
                    _accept_slot_used = True
                if self.create_by in _MAIN_PERSONA_CREATE_BY:
                    _fact_pending = bool(
                        st.saw_structured_return and not st.delegated_render and not st.image_sent_this_run
                    )
                    _blk, _why = should_block_user_visible_text(
                        st.speech_policy,
                        _text,
                        pending_async=st.pending_async_delivery,
                        image_sent=st.image_sent_this_run,
                        has_status_tool=st.has_status_tool_call,
                        tool_calls_so_far=st.tool_call_list,
                        wait_comfort_sent=st.wait_comfort_sent,
                        fact_pack_pending=_fact_pending,
                        has_active_task=st.has_active_task,
                        render_inflight=bool(st.delegated_render and not st.image_sent_this_run),
                        speech_len_hard=_hard,
                        user_asked_detail=False,
                    )
                    if _blk:
                        # 只记排版失配；**不得**回写 saw_structured_return（那是出处凭据，
                        # 由真实 ToolReturn 置位）。伪造它会让纯文本长回答被当成待出图事实包。
                        if _why in ("report_speech", "empty_handoff", "persona_length_breach"):
                            st.presentation_mismatch = True
                            # 暂扣原文：纠正被申辩/无替代品时由 settle 兜底发出（INV-4）
                            if _text not in st.presentation_withheld:
                                st.presentation_withheld.append(_text)
                                st.presentation_withheld_reasons.append(_why)
                        elif _why == "numeric_recitation":
                            # 念数丢掉、不进 INV-4；记原因以便 settle 走 render 纠正。
                            st.presentation_mismatch = True
                            st.presentation_withheld_reasons.append(_why)
                        logger.info(
                            i18n_t(
                                "log.agent.silent_skipping_text",
                                _text=f"[speech_policy={st.speech_policy}/{_why}] {_text[:60]}",
                            )
                        )
                        if _slot == "send_accept":
                            _resp_unsent.append(_text)
                        self._discard_stream_preview(st, _text)
                        continue
                    _inflight_now = bool(
                        st.pending_async_delivery
                        or st.speech_policy == "silence_only"
                        or (st.delegated_render and not st.image_sent_this_run)
                    )
                    _is_wait_comfort = looks_like_wait_comfort(_text)
                    _accept = looks_like_task_accept_speech(_text, max_len=_hard)
                    # 接任务应或在途短应都只占一次，避免再发「等数据回来」。
                    if _is_wait_comfort or (_accept and (_inflight_now or _saw_tool_call_this_turn)):
                        st.wait_comfort_sent = True
                    # 砍掉「要不要我再查」类助理收尾，保留事实句
                    _text = strip_open_solicitations(_text)
                    if not _text.strip():
                        self._discard_stream_preview(st)
                        continue
                if st.bot and _text and st.return_mode in ["always", "by_bot"]:
                    # 统一输出闸门；同 response 尖括号只计一次 attempt
                    _user_raw = st.ev.raw_text if st.ev is not None and st.ev.raw_text else ""
                    _count_ab = not _ab_attempt_counted_this_response
                    _gr = output_gate.pre_send_gate(
                        _text,
                        _require_context(st).extra,
                        user_text=_user_raw,
                        channel="main",
                        count_attempt=_count_ab,
                    )
                    if _gr.decision is output_gate.GateDecision.FUSE:
                        st.ab_abort = True
                        _ab_attempt_counted_this_response = True
                        logger.warning(
                            i18n_t(
                                "log.ai.output_gate_drop_text_after_fuse",
                                policy=_gr.policy,
                                preview=repr(_text[:80]),
                            )
                        )
                        if _slot == "send_accept":
                            _resp_unsent.append(_text)
                        self._discard_stream_preview(st, _text)
                        continue
                    if _gr.decision is output_gate.GateDecision.REWRITE:
                        if _gr.defer_ooc and _gr.ooc_hit is not None:
                            logger.warning(
                                i18n_t(
                                    "log.agent.firewall_main_output_hit_ooc",
                                    p0=_gr.ooc_hit.category,
                                    p1=_gr.ooc_hit.matched,
                                )
                            )
                            st.ooc_blocked.append((_text, _gr.ooc_hit))
                        else:
                            if _gr.policy == "angle_bracket":
                                _ab_attempt_counted_this_response = True
                            if _gr.feedback:
                                st.ab_pending_nudges.append(_gr.feedback)
                            if _gr.fused:
                                st.ab_abort = True
                        if _slot == "send_accept":
                            _resp_unsent.append(_text)
                        self._discard_stream_preview(st, _text)
                        continue
                    if _gr.decision is output_gate.GateDecision.FALLBACK:
                        self._discard_stream_preview(st, _text)
                        _fb = _gr.send_text or output_firewall.fallback_machine_text(self.persona_name)
                        try:
                            await send_chat_result(st.bot, _fb, ev=st.ev, ooc_check=False)
                            self._run_sent_texts.add(_fb)
                        except Exception as _me:
                            logger.debug(i18n_t("log.agent.text_send_fail_failed", _e=_me))
                        if _slot == "send_accept":
                            _resp_unsent.append(_text)
                        continue
                    # 假完成预检：完成声明 + 本轮没有生效的写入（含 PIN 拒绝）
                    _fab_gate_on = not st.fake_done_retry and not st.effectual_mutate and bool(st.tool_names)
                    if _fab_gate_on and _claims_fake_done(_text):
                        logger.warning(i18n_t("log.agent.fakedone_zero_claim_pending_ok", p0=repr(_text[:40])))
                        st.fab_blocked.append(_text)
                        if _slot == "send_accept":
                            _resp_unsent.append(_text)
                        self._discard_stream_preview(st, _text)
                        continue
                    # 单轮出站配额兜底（4.10）：主通道台词超限即静默，防多 TextPart 刷屏
                    _cap = int(ai_config.get_config("main_channel_visible_limit").data)
                    if _cap < 1:
                        _cap = MAIN_CHANNEL_VISIBLE_LIMIT
                    if st.main_channel_sends >= _cap:
                        logger.info(
                            i18n_t(
                                "log.agent.silent_skipping_text",
                                _text=f"[main_channel_cap] {_text[:40]}",
                            )
                        )
                        _resp_unsent.append(_text)
                        self._discard_stream_preview(st, _text)
                        continue
                    # Why: send_chat_result 抛异常会穿透 _agent.iter() 的 async st.context 触发
                    # athrow/cancel scope
                    try:
                        _extra = _require_context(st).extra
                        _at_uid = _extra["at_user_id"] if "at_user_id" in _extra else None
                        _at = str(_at_uid) if isinstance(_at_uid, str) and _at_uid else None
                        await self._send_gated_text(st, _text, at_user_id=_at)
                    except Exception as _e:
                        logger.debug(i18n_t("log.agent.text_send_fail_failed", _e=_e))

            elif isinstance(part, ThinkingPart):
                _thinking = part.content.strip()
                logger.debug(i18n_t("log.agent.llm_thinking", _thinking=_thinking))
                if _thinking:
                    st.thinking_segments.append(_thinking)
                self._session_logger.log_thinking(_thinking)
                # 流式 event 已按 delta 推过则不要整段再打一遍
                if _thinking and not st.thinking_streamed:
                    self._emit_trace("thinking", _thinking)

        if _resp_unsent:
            st.unsent_texts.extend(_resp_unsent)
            _unsent_left = list(_resp_unsent)
            _kept_parts = []
            for _p in node.model_response.parts:
                if _unsent_left and isinstance(_p, TextPart) and _p.content.strip() == _unsent_left[0]:
                    _unsent_left.pop(0)
                    continue
                _kept_parts.append(_p)
            node.model_response.parts = _kept_parts

        # thrash：本响应只按「轮」计 1 次（并行多 query 不累加）
        st.same_tool_name, st.same_tool_streak = _update_thrash_streak_for_response(
            _resp_tool_names,
            prev_name=st.same_tool_name,
            prev_streak=st.same_tool_streak,
        )

        # 结算本轮模型请求的性能统计： TTFT = 请求发起 → 首个流式 event；
        _ttft_ms: float = 0.0
        _tps: float = 0.0
        _req_usage = node.model_response.usage
        if st.first_event_at is not None and st.last_event_at is not None:
            _ttft_ms = round((st.first_event_at - st.req_start) * 1000, 2)
            _generation_time = st.last_event_at - st.first_event_at
            if _req_usage.output_tokens > 0 and _generation_time > 0:
                _tps = round(_req_usage.output_tokens / _generation_time, 2)
            logger.debug(i18n_t("log.ai_agent.ttft_ms_tps_tokens_ok", ttft_ms=_ttft_ms, tps=_tps))
        statistics_manager.record_hourly_performance(
            provider=st.provider,
            model_name=st.model_name,
            ttft_ms=_ttft_ms,
            tps=_tps,
            input_tokens=_req_usage.input_tokens,
            output_tokens=_req_usage.output_tokens,
            cache_read_tokens=_req_usage.cache_read_tokens,
            cache_write_tokens=_req_usage.cache_write_tokens,
            tool_call_count=sum(1 for p in node.model_response.parts if isinstance(p, ToolCallPart)),
        )
        # 复位打点，避免异常路径下两轮请求的数据串台
        st.first_event_at = None
        st.last_event_at = None

    async def _run_once_iter_and_settle(
        self,
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
                # 遍历每一步 Node
                async for node in agent_run:
                    # A: 节点间隙检查抢答取消（后到消息已请求 abort）
                    if st.cancel_ev is not None and st.cancel_ev.is_set():
                        st.generation_cancelled = True
                        logger.info(i18n_t("log.agent.generation_cancelled_supersede"))
                        break
                    # 1. 发起大模型请求前的处理
                    if isinstance(node, ModelRequestNode):
                        await self._run_once_on_model_request(st, node, agent_run)
                        if st.generation_cancelled:
                            break

                    # 2. 获取到大模型响应，准备调用工具或者输出文本 这里使用了 isinstance
                    # Pyright 就能明确知道此时 node 是 CallToolsNode 拥有 model_response 属性
                    elif isinstance(node, CallToolsNode):
                        await self._run_once_on_call_tools(st, node, statistics_manager)

                    # 3. 运行结束节点
                    elif isinstance(node, End):
                        logger.debug(i18n_t("log.agent.node_trigger_end"))
                        logger.debug(i18n_t("log.agent.run_ended_final_result_generated"))
                        self._session_logger.log_node_transition("End")

            # A: 被 supersede 打断 → 不写 history、不 OOC 重说，让后到 run 用完整上下文重生成。
            # 在途委派不丢：根任务在库里，下轮由 build_task_context 注入，产物经邮箱回灌。
            if st.generation_cancelled:
                logger.info(i18n_t("log.agent.generation_aborted_no_history"))
                return "" if st.output_type is None else None

            # 遍历完成后收尾
            return await self._run_once_settle_result(st, agent_run, statistics_manager)
        finally:
            self._history_iter_active = False
