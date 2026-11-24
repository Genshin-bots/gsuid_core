"""阶段 D：收尾（history/闸门/假完成）+ UsageLimit 兜底 + finally 清理"""

from __future__ import annotations

import re
import time
import asyncio
from typing import Any, List, Sequence
from datetime import datetime

from pydantic_ai import Agent
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai.messages import (
    TextPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
)
from pydantic_ai.settings import ModelSettings

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core import wall_clock, output_gate
from gsuid_core.ai_core.const import (
    _STICKY_FAMILY_TURNS,
    _PROGRESSIVE_TOOLS_SKIP_INTENTS,
)
from gsuid_core.ai_core.delivery import send_chat_result
from gsuid_core.ai_core.register import find_tool_base
from gsuid_core.ai_core.agent_errors import (
    NO_RESULT_TEXT,
    strip_framework_user_leaks,
)
from gsuid_core.ai_core.protocol_text import is_silence_marker
from gsuid_core.ai_core.agent_run.host import RunOnceHost
from gsuid_core.ai_core.agent_run.state import (
    ReturnMode,
    RunOnceState,
    _require_limits,
    _require_context,
)
from gsuid_core.ai_core.history_compact import _relean_user_turn
from gsuid_core.ai_core.agent_run.support import (
    _THRASH_FUSE_NUDGE,
    record_output_block,
    turn_reply_metadata,
    collect_run_thinking,
    _correction_nudge_markers,
    absorb_run_image_delivered,
    usage_limit_return_payload,
)
from gsuid_core.ai_core.control.directive import (
    Directive,
    render_control_envelope,
)
from gsuid_core.ai_core.angle_bracket_guard import strip_protocol_tags
from gsuid_core.ai_core.control.policy_ring import (
    SettleRingInput,
    turn_user_text,
    corrected_or_original,
    run_settle_policy_ring,
)
from gsuid_core.ai_core.agent_run.budget_ctx import reset_user_turn_id, _current_budget_scope
from gsuid_core.ai_core.control.outbound_detectors import (
    is_tech_dump,
    inspect_outbound_ooc,
    is_ooc_firewall_enabled,
)

# 收口交付的第一行。不以能力代理失败前缀开头，看板才会把这一步标完成。
_USAGE_LIMIT_SALVAGE_BANNER = "⚠ 思考轮数已到上限。下面是根据已收集材料收口的交付，未继续补查。"
_SALVAGE_SKIP_TOOLS = frozenset(
    {
        "read_handle",
        "list_skills",
        "artifact_get",
        "artifact_get_recent",
        "artifact_list",
    }
)
_SALVAGE_ITEM_CAP = 2000
_SALVAGE_TOTAL_CAP = 12000
_SALVAGE_ROW_LIMIT = 10


async def _deliver_withheld(st: RunOnceState, sent: set[str], sent_order: list[str] | None = None) -> None:
    """把排版闸暂扣的原文真正发给用户（INV-4 兜底，只发第一段防刷屏）。"""
    for body in st.presentation_withheld:
        if not body or body in sent:
            continue
        if st.bot is None:
            return
        await send_chat_result(st.bot, body, ev=st.ev, extra_metadata=turn_reply_metadata(st.ev))
        absorb_run_image_delivered(st)
        sent.add(body)
        if sent_order is not None and body not in sent_order:
            sent_order.append(body)
        return


def _absorb_attempt_facts(
    st: RunOnceState,
    *,
    tool_calls: Sequence[str],
    delegated_render: bool,
    image_sent: bool,
    pending_async: bool,
    effectual_mutate: bool = False,
    has_status_tool: bool,
) -> None:
    """把纠正轮写在宿主上的结构事实并回父 st（纠正轮是一份新 RunOnceState）。"""
    for name in tool_calls:
        if name not in st.tool_call_list:
            st.tool_call_list.append(name)
    if delegated_render:
        st.delegated_render = True
    if image_sent:
        st.image_sent_this_run = True
    if pending_async:
        st.pending_async_delivery = True
    if effectual_mutate:
        st.effectual_mutate = True
    if has_status_tool:
        st.has_status_tool_call = True


def _should_deliver_withheld(
    st: RunOnceState,
    *,
    skip_report_exit: bool,
    replacement_visible: bool,
) -> bool:
    """INV-4：暂扣原文在纠正未兑现、且没有可交付替代时必须发出。"""
    if not st.presentation_withheld or replacement_visible or st.bot is None:
        return False
    if st.return_mode not in ("always", "by_bot"):
        return False
    if st.image_sent_this_run or st.delegated_render or st.pending_async_delivery:
        return False
    if skip_report_exit:
        return True
    # 未进纠正（短 empty_handoff）时不能只靠 _skip_report_exit，否则整轮零输出
    return bool(st.saw_structured_return)


_DLG_ROOT_RE = re.compile(r"dlg_([0-9a-fA-F-]{8,})")


def _dlg_root_of(msg: object) -> str:
    """UserPromptPart 里的 dlg_ 句柄 root；无则空串。"""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    if not isinstance(msg, ModelRequest):
        return ""
    for part in msg.parts:
        if isinstance(part, UserPromptPart) and isinstance(part.content, str):
            m = _DLG_ROOT_RE.search(part.content)
            if m is not None:
                return m.group(1)
    return ""


def _dedupe_delivery_cards(messages: List[ModelMessage]) -> List[ModelMessage]:
    """同 root 的 dlg_ 交付包只留最新一条（超时回执 + 事后交付会重复）。"""
    seen: set[str] = set()
    kept: List[ModelMessage] = []
    for msg in reversed(messages):
        root = _dlg_root_of(msg)
        if root and root in seen:
            continue
        if root:
            seen.add(root)
        kept.append(msg)
    kept.reverse()
    return kept


def _drop_unsent_text_from_tail(messages: List[ModelMessage], unsent: Sequence[str]) -> List[ModelMessage]:
    """从本轮 new_messages 尾部剥掉未出站 TextPart（只动尾）。"""
    if not unsent:
        return messages
    pending = [u.strip() for u in unsent if u.strip()]
    if not pending:
        return messages
    out: List[ModelMessage] = []
    for msg in reversed(messages):
        if pending and isinstance(msg, ModelResponse):
            kept = []
            for part in reversed(msg.parts):
                if pending and isinstance(part, TextPart) and part.content.strip() == pending[-1]:
                    pending.pop()
                    continue
                kept.append(part)
            kept.reverse()
            msg.parts = kept
        out.append(msg)
    out.reverse()
    return out


async def load_session_salvage_material(session_id: str) -> str:
    """本会话最近落盘的检索原文，新的在前。库失败时返回空串，改用本轮摘录。"""
    if not session_id:
        return ""
    try:
        return await _read_session_salvage_material(session_id)
    except Exception as exc:
        logger.warning(i18n_t("log.agent.usage_limit_salvage_sources_fail", e=exc))
        return ""


async def _read_session_salvage_material(session_id: str) -> str:
    import asyncio

    from gsuid_core.ai_core.planning.tool_output_store import AIToolOutputRecord
    from gsuid_core.ai_core.planning.tool_output_protocol import load_payload_text

    rows = await AIToolOutputRecord.list_recent(session_id=session_id, limit=_SALVAGE_ROW_LIMIT)
    chunks: list[str] = []
    used = 0
    for rec in rows:
        if rec.tool_name in _SALVAGE_SKIP_TOOLS:
            continue
        text, _err = await asyncio.to_thread(
            load_payload_text,
            payload_inline=rec.payload_inline,
            payload_path=rec.payload_path,
        )
        body = text.strip()
        if not body:
            continue
        block = f"[{rec.tool_name}] {body[:_SALVAGE_ITEM_CAP]}"
        if used + len(block) > _SALVAGE_TOTAL_CAP:
            remain = _SALVAGE_TOTAL_CAP - used
            if remain < 200:
                break
            block = block[:remain]
        chunks.append(block)
        used += len(block)
        if used >= _SALVAGE_TOTAL_CAP:
            break
    return "\n\n".join(chunks)


class SettlePhase(RunOnceHost):
    @property
    def sent_texts(self) -> set[str]:
        return self._run_sent_texts

    @property
    def dispute_count(self) -> int:
        return len(self._run_disputes)

    def latest_dispute_preview(self) -> str:
        return self._run_disputes[-1][:120] if self._run_disputes else ""

    @property
    def attempt_tool_calls(self) -> Sequence[str]:
        return self._last_attempt_tool_calls

    def model_declares_video(self) -> bool:
        return self._model_declares_video()

    async def try_correction_pass(
        self,
        st: RunOnceState,
        directives: tuple[Directive, ...],
        *,
        suppress_intermediate_text: bool = True,
        return_mode: ReturnMode = "return",
    ) -> object:
        return await self._try_correction_pass(
            st,
            directives,
            suppress_intermediate_text=suppress_intermediate_text,
            return_mode=return_mode,
        )

    async def emit_unsent_reply(self, st: RunOnceState, text: str) -> None:
        await self._emit_unsent_reply(st, text)

    async def run_inv3_correction(
        self,
        st: RunOnceState,
        directive: Directive,
        result_msg: str,
        *,
        scrub_replaced: bool = True,
    ) -> str:
        return await self._run_inv3_correction(
            st,
            directive,
            result_msg,
            scrub_replaced=scrub_replaced,
        )

    def scrub_fake_done_history(self, fabricated: set[str]) -> None:
        self._scrub_fake_done_history(fabricated)

    def note_output_block(self, st: RunOnceState, reason: str, body: str) -> None:
        record_output_block(st, self._session_logger, reason, body)

    async def _try_correction_pass(
        self,
        st: RunOnceState,
        directives: tuple[Directive, ...],
        *,
        suppress_intermediate_text: bool | None = None,
        return_mode: ReturnMode | None = None,
    ) -> object:
        """纠正重跑；失败返回 None，原答案按 INV-3 生效。
        成功时把本轮工具事实并回父级，失败不并回。"""
        _suppress = st.suppress_intermediate_text if suppress_intermediate_text is None else suppress_intermediate_text
        # 话术纠正要拿回文本：by_bot 成功路径会 return ""，父级补发看不到改写。
        _mode = st.return_mode if return_mode is None else return_mode
        # Why: 纠正是增强路径，失败不得毁掉已完成的用户轮（INV-3）
        try:
            corrected = await self._execute_run_once(
                user_message=render_control_envelope(directives),
                bot=st.bot,
                ev=st.ev,
                tools=st.tools,
                return_mode=_mode,
                intent=st.intent,
                has_active_task=st.has_active_task,
                suppress_intermediate_text=_suppress,
                fake_done_retry=True,
                is_framework_injection=True,
                injection_reason=",".join(d.reason_code for d in directives if d.reason_code),
            )
        except Exception as _fe:
            logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_fe))
            # 失败时指针已指向纠正轮的空列表，拨回父级，否则对外报成没调过工具。
            self._last_attempt_tool_calls = st.tool_call_list
            return None
        _absorb_attempt_facts(
            st,
            tool_calls=self._last_attempt_tool_calls,
            delegated_render=self._last_attempt_delegated_render,
            image_sent=self._last_attempt_image_sent,
            pending_async=self._last_attempt_pending_async,
            effectual_mutate=self._last_attempt_effectual_mutate,
            has_status_tool=self._last_attempt_has_status_tool,
        )
        self._last_attempt_tool_calls = st.tool_call_list
        return corrected

    async def _emit_unsent_reply(self, st: RunOnceState, text: str) -> None:
        """纠正拿回的句子由本层发出。return 模式不会在内层再过话术闸。"""
        body = text.strip()
        if not body or is_silence_marker(body) or body in self._run_sent_texts:
            return
        if st.bot is None or st.return_mode not in ("always", "by_bot"):
            return
        raw_at = st.run_extra["at_user_id"] if "at_user_id" in st.run_extra else None
        await send_chat_result(
            st.bot,
            body,
            ev=st.ev,
            at_user_id=str(raw_at) if isinstance(raw_at, str) and raw_at else None,
            extra_metadata=turn_reply_metadata(st.ev),
        )
        absorb_run_image_delivered(st)
        self._run_sent_texts.add(body)
        if body not in self._run_sent_order:
            self._run_sent_order.append(body)
        if st.main_channel_sends == 0:
            st.main_channel_sends = 1

    async def _run_inv3_correction(
        self,
        st: RunOnceState,
        directive: Directive,
        result_msg: str,
        *,
        scrub_replaced: bool = True,
    ) -> str:
        """标准 INV-3 纠正重跑：产出可交付新文本则替换、下发并剥旧答，否则保留原答。"""
        logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
        prior = result_msg.strip()
        corrected = await self._try_correction_pass(
            st,
            (directive,),
            suppress_intermediate_text=True,
            return_mode="return",
        )
        next_msg = corrected_or_original(corrected, original=result_msg, user_text=turn_user_text(st))
        if next_msg.strip() != prior:
            await self._emit_unsent_reply(st, next_msg)
            self._scrub_fake_done_history({prior} if (scrub_replaced and prior) else set())
        else:
            self._scrub_fake_done_history(set())
        return next_msg

    def _record_prefix_break_probe(self, st: RunOnceState, new_msgs: List[ModelMessage]) -> None:
        """对比上一 run 发送快照与当前 history 头，记 prefix_break_reason。"""
        from gsuid_core.ai_core.prefix_probe import (
            PrefixSnapshot,
            tools_diff as _tools_diff,
            hash_tool_names,
            history_payloads,
            hash_system_prompt,
            record_prefix_break,
            hash_history_messages,
            classify_prefix_break_with_pos,
        )

        tools_hash = hash_tool_names(st.tool_names)
        system_hash = hash_system_prompt(self.system_prompt or "")
        hist_hashes = hash_history_messages(self.history)
        prev = self._prefix_snapshot
        reason, break_pos, approx_tokens_before = classify_prefix_break_with_pos(
            prev,
            history_hashes=hist_hashes,
            tools_hash=tools_hash,
            system_hash=system_hash,
            prev_payloads=prev.payloads if prev is not None else (),
            curr_payloads=history_payloads(self.history),
        )
        diff: dict[str, list[str]] | None = None
        if prev is not None and reason == "tools":
            diff = _tools_diff(prev.tool_names, st.tool_names)
            removed = diff["removed"] if "removed" in diff else []
            if removed:
                logger.warning(i18n_t("log.agent.prefix_tools_removed", names=removed[:12]))
        record_prefix_break(reason)
        self._session_logger.log_prefix_break(
            reason,
            tools_hash=tools_hash,
            system_hash=system_hash,
            tools_diff=diff,
            break_pos=break_pos,
            approx_tokens_before=approx_tokens_before,
        )
        combined = list(self.history) + list(new_msgs)
        self._prefix_snapshot = PrefixSnapshot(
            history_hashes=hash_history_messages(combined),
            tools_hash=tools_hash,
            system_hash=system_hash,
            payloads=history_payloads(combined),
            tool_names=list(st.tool_names),
        )

    async def _run_once_settle_result(
        self,
        st: RunOnceState,
        agent_run: Any,
        statistics_manager: Any,
    ) -> Any:
        """iter 成功路径：history / 闸门 / token / 假完成 / OOC / return。"""
        absorb_run_image_delivered(st)
        # 遍历完成后，直接从 agent_run 中获取最终结果
        result = agent_run.result
        if result:
            logger.info(i18n_t("log.agent.iter_ok"))

            # 先记本轮真实发给模型的前缀快照，再把 user 侧还原为精简版落盘（§优化 O-1）。
            _new_msgs = result.new_messages()
            self._record_prefix_break_probe(st, _new_msgs)
            _relean_user_turn(
                _new_msgs,
                st.lean_user_message,
                strip_hint_texts=(
                    _THRASH_FUSE_NUDGE,
                    *output_gate.GATE_NUDGE_MARKERS,
                    *_correction_nudge_markers(),
                ),
            )
            _new_msgs = [m for m in _new_msgs if not (isinstance(m, ModelRequest) and len(m.parts) == 0)]
            _new_msgs = _dedupe_delivery_cards(_new_msgs)
            _new_msgs = _drop_unsent_text_from_tail(_new_msgs, st.unsent_texts)

            # 前缀缓存红线（§1.7）：主会话禁止事后改写已入缓存的 ToolReturnPart；
            # 超大回执仅在 compact_session_history 截断尾段，或 CapabilityAgent 长跑
            # 触发 fold_stale_capability_tool_returns（仅计可折叠旧段）成批折叠。
            self.history.extend(_new_msgs)
            _ctx_dyn = st.context
            if _ctx_dyn is not None:
                self._last_run_find_tools_names = sorted(_ctx_dyn.dynamic_tool_names)

            # 输出闸门收尾：尖括号熔断/补写/scrub；熔断后仍做独立 OOC 重说
            st.ab_abort = await self._resolve_output_gate_after_run(
                _require_context(st),
                st.bot,
                st.ev,
                return_mode=st.return_mode,
                ooc_blocked=st.ooc_blocked,
                ab_abort=st.ab_abort or output_gate.is_fused(_require_context(st).extra),
            )

            # L3：记录本轮实际调用过的工具所属能力族，使其在随后数轮继续常驻，
            if st.tool_call_list:
                for _tname in set(st.tool_call_list):
                    _tb = find_tool_base(_tname)
                    _dom = _tb.capability_domain if _tb else None
                    if _dom:
                        self._recent_tool_families[_dom] = _STICKY_FAMILY_TURNS

            # 更新连续无工具调用计数（仅对交互式主 Agent 生效）。闲聊类意图不计数（§15）
            # 豁免口径与注入门同源：_PROGRESSIVE_TOOLS_SKIP_INTENTS（评审修复 E12）。
            if self.create_by in ["Chat", "Agent"] and st.intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS:
                if st.tool_call_list:
                    self._consecutive_no_tool_rounds = 0
                    self._unmet_tool_promise = False
                else:
                    self._consecutive_no_tool_rounds += 1
                    # 思考里点了本轮已装配的工具名却没调用。只记这一笔，不因空轮催工具。
                    thinking_blob = "\n".join(st.thinking_segments)
                    mentioned = bool(
                        thinking_blob and st.tool_names and any(tn in thinking_blob for tn in st.tool_names)
                    )
                    self._unmet_tool_promise = mentioned
                    if mentioned:
                        logger.debug(i18n_t("log.agent.intent_action_mismatch_force"))

            # 记录 Token 使用量和延迟统计
            # 记录响应延迟
            latency = time.time() - st.start_time
            statistics_manager.record_latency(latency=latency)

            try:
                # v2: result.usage / result.timestamp 由方法改为属性
                usage_obj: RunUsage = result.usage
                input_tokens: int = usage_obj.input_tokens
                output_tokens: int = usage_obj.output_tokens
                cache_read_tokens: int = usage_obj.cache_read_tokens
                cache_write_tokens: int = usage_obj.cache_write_tokens

                logger.info(
                    i18n_t(
                        "log.agent.token_usage_input_tokens",
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read_tokens,
                        cache_write_tokens=cache_write_tokens,
                    )
                )

                # TTFT/TPS 已在 CallToolsNode 按请求结算。这里只记 run 级 token。
                # token 为 0 仍计数，避免漏掉分母。
                _is_nested = bool(self.is_subagent) or (bool(st.user_turn_id) and not st.owns_user_turn)
                if input_tokens > 0 or output_tokens > 0:
                    statistics_manager.record_token_usage(
                        model_name=st.model_name,
                        chat_type=st.stats_chat_type if st.stats_chat_type else self.create_by,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read_tokens,
                        cache_write_tokens=cache_write_tokens,
                    )
                statistics_manager.record_agent_run(
                    owns_user_turn=bool(st.owns_user_turn),
                    under_user_turn=bool(st.user_turn_id),
                    is_nested=_is_nested,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                )
                # 预算记账：可归属 scope 的 run 计入对应 Session 额度，无 scope 只进全局
                # 统计。独立 try 且先于 session 日志，避免日志抛错把整笔记账一起跳过。
                if st.budget_scope is not None:
                    try:
                        from gsuid_core.ai_core.budget import budget_manager

                        await budget_manager.record_usage_scope(
                            st.budget_scope[0],
                            st.budget_scope[1],
                            st.budget_scope[2],
                            self.session_id,
                            input_tokens,
                            output_tokens,
                            cache_read_tokens,
                            cache_write_tokens,
                        )
                    except Exception as _be:
                        logger.warning(i18n_t("log.agent.budget_fail", _be=_be))
                try:
                    self._session_logger.log_token_usage(
                        input_tokens,
                        output_tokens,
                        st.model_name,
                        cache_read_tokens,
                        cache_write_tokens,
                    )
                except Exception as _le:
                    logger.debug(i18n_t("log.agent.write_token_usage_log", _le=_le))
            except AttributeError as e:
                # result 没有 usage 属性（如 pydantic_graph End 节点返回的结果）
                logger.info(i18n_t("log.agent.access_result_usage", e=e))
                pass
            except TypeError as e:
                # v1 旧写法 result.usage() 在 v2 抛 'RunUsage' is not callable
                logger.info(i18n_t("log.agent.result_usage_call_style", e=e))
                pass
            except Exception as e:
                logger.warning(i18n_t("log.agent.record_statistics", e=e))

            # 当 return_model 指定时，直接返回 Pydantic 模型实例
            if st.output_type is not None:
                self._session_logger.log_run_end()
                self._session_logger.log_result(result.output, st.tool_call_list)
                return result.output

            # 始终返回字符串类型
            result_msg = str(result.output).strip()
            # 协议标签（<reply #n>）只供出站路由，判定/匹配一律用剥签文本。
            _match_msg = strip_protocol_tags(result_msg).strip()
            # 工具调用列表只进调试日志，不追加到用户可见消息
            if st.tool_call_list:
                logger.debug(i18n_t("log.agent.current_tool_call_event", p0=", ".join(st.tool_call_list)))

            self._session_logger.log_run_end()
            # result 在出口消毒后再记，避免 raw 念数被当成已出站。

            # 人格花名册无 render_agent 时，出图义务/文案整体降级为口头交付
            from gsuid_core.ai_core.agent_node.registry import persona_allows_capability_agent

            _allow_render = (
                not ("delivery_failed" in st.run_extra and st.run_extra["delivery_failed"] is True)
            ) and persona_allows_capability_agent(self.persona_name, "render_agent")

            async def _resend_fab_blocked() -> None:
                for _bt in st.fab_blocked:
                    if _bt in self._run_sent_texts:
                        continue
                    try:
                        if st.bot is None:
                            logger.warning(i18n_t("log.agent.fakedone_bot_object_unavailable"))
                            continue
                        await send_chat_result(st.bot, _bt, ev=st.ev, extra_metadata=turn_reply_metadata(st.ev))
                        absorb_run_image_delivered(st)
                        self._run_sent_texts.add(_bt)
                        if _bt not in self._run_sent_order:
                            self._run_sent_order.append(_bt)
                    except Exception as _se:
                        logger.debug(i18n_t("log.agent.fakedone_se", _se=_se))

            _ring_out = await run_settle_policy_ring(
                self,
                SettleRingInput(
                    st=st,
                    result_msg=result_msg,
                    match_msg=_match_msg,
                    allow_render=_allow_render,
                    resend_fab_blocked=_resend_fab_blocked,
                ),
            )
            result_msg = _ring_out.result_msg
            _skip_report_exit = _ring_out.skip_report_exit
            _replacement_visible = _ring_out.replacement_visible

            # INV-4：暂扣原文在纠正未兑现（或根本没进纠正）且没有替代品时必须发出。
            if _should_deliver_withheld(
                st,
                skip_report_exit=_skip_report_exit,
                replacement_visible=_replacement_visible,
            ):
                await _deliver_withheld(st, self._run_sent_texts, self._run_sent_order)

            if st.return_mode in ["by_bot"] and st.bot and st.ev:
                self._record_settle_journal(st, result_msg)
                self._log_run_result(st, result_msg)
                return ""
            # 对用户可见出口才做 roleplay OOC；子代理/能力代理 return 必须保留 res_ 句柄
            if result_msg and is_ooc_firewall_enabled() and self.create_by not in ("EvalJudge", "TEST"):
                _skip_roleplay_scrub = self.is_subagent or self.create_by in (
                    "CapabilityAgent",
                    "AutoPlanner",
                )
                if _skip_roleplay_scrub:
                    if is_tech_dump(_match_msg):
                        result_msg = "⚠️ 子任务返回技术错误堆栈，已屏蔽。请主人格换路或重试（勿向用户念本句）。"
                else:
                    # 框架不替人格说话：命中就让人格自己重说一句，重说不出来就沉默
                    _hit = inspect_outbound_ooc(
                        _match_msg,
                        user_text=st.ev.raw_text if st.ev is not None and st.ev.raw_text else "",
                    )
                    if _hit is not None:
                        logger.warning(
                            i18n_t(
                                "log.agent.firewall_run_return_value_hit",
                                p0=_hit.category,
                            )
                        )
                        _ooc_original = result_msg
                        recovered = await self._ooc_recover_persona_voice(_hit, result_msg, st.ev) or "<SILENCE>"
                        had_body = bool(_ooc_original.strip()) and not is_silence_marker(_ooc_original.strip())
                        if had_body and (not recovered.strip() or is_silence_marker(recovered.strip())):
                            st.silence_reason = "ooc_recover"
                            record_output_block(st, self._session_logger, "ooc_recover", _ooc_original)
                        result_msg = recovered
            if isinstance(result_msg, str) and not (
                self.is_subagent or self.create_by in ("CapabilityAgent", "AutoPlanner")
            ):
                _leak_original = result_msg
                leaked = strip_framework_user_leaks(result_msg)
                had_body = bool(_leak_original.strip()) and not is_silence_marker(_leak_original.strip())
                result_msg = leaked
                if not result_msg.strip():
                    if had_body:
                        st.silence_reason = "framework_leak_stripped"
                        record_output_block(st, self._session_logger, "framework_leak_stripped", _leak_original)
                    result_msg = "<SILENCE>"
            if isinstance(result_msg, str):
                self._record_settle_journal(st, result_msg)
            result_msg = self._attach_unfinished_tool_return(st, result_msg)
            self._log_run_result(st, result_msg)
            if isinstance(result_msg, str):
                self._remember_outbound_on_silence(st, result_msg)
            return result_msg

        # result 为空时的默认返回值（常量：handle_ai 好感度门等消费端按它识别准失败轮）
        return NO_RESULT_TEXT

    def _record_settle_journal(self, st: RunOnceState, final_msg: str) -> None:
        """在策略环、OOC 防火墙与协议泄漏剥离全部通过后，把合规实质行动写入驻群手账。"""
        if st.ab_abort or st.ev is None or st.ev.user_type == "direct" or not st.ev.group_id:
            return
        skip_role = self.is_subagent or self.create_by in (
            "Aside",
            "Autonomy",
            "Autonomy_Output",
            "Heartbeat_Decision",
            "Heartbeat_Output",
            "Journal_Compact",
            "CapabilityAgent",
            "AutoPlanner",
            "EvalJudge",
        )
        if skip_role:
            return

        from gsuid_core.message_history import get_history_manager
        from gsuid_core.ai_core.register import find_tool_base
        from gsuid_core.ai_core.inbox.turn import current_turn
        from gsuid_core.ai_core.journal.manager import record_journal_entry
        from gsuid_core.ai_core.journal.storage import JournalEntry
        from gsuid_core.ai_core.journal.sanitizer import (
            clean_journal_tag,
            is_rejected_journal_body,
            sanitize_journal_summary,
        )

        meta_tools = frozenset(
            {"find_tools", "call_tool", "search_cognition", "read_handle", "read_life_journal", "dispute_directive"}
        )
        substantive_tools = [tn for tn in (st.tool_call_list or []) if tn not in meta_tools]
        ordered_bubbles: list[str] = [
            t_s.strip() for t_s in self._run_sent_order if t_s.strip() and "<SILENCE>" not in t_s
        ]
        for extra_txt in sorted(self._run_sent_texts):
            clean_extra = extra_txt.strip()
            if clean_extra and "<SILENCE>" not in clean_extra and clean_extra not in ordered_bubbles:
                ordered_bubbles.append(clean_extra)

        if st.return_mode in ("by_bot",) and st.bot is not None:
            # by_bot 模式以已通过各层闸门真实出站的文本为准，绝不读取未出站的原始 result.output
            if ordered_bubbles:
                raw_out = "；".join(ordered_bubbles)
            elif st.image_sent_this_run:
                raw_out = "已完成群内图片交付"
            else:
                return
        else:
            tail_msg = final_msg.strip()
            valid_tail = (
                bool(tail_msg)
                and not is_silence_marker(tail_msg)
                and "<SILENCE>" not in tail_msg
                and not is_rejected_journal_body(tail_msg)
            )
            if ordered_bubbles:
                combined = list(ordered_bubbles)
                if valid_tail and tail_msg not in combined:
                    combined.append(tail_msg)
                raw_out = "；".join(combined)
            elif valid_tail:
                raw_out = tail_msg
            elif st.image_sent_this_run:
                raw_out = "已完成群内图片交付"
            else:
                return

        if not raw_out or is_silence_marker(raw_out) or is_rejected_journal_body(raw_out):
            return

        nicks_for_j: dict[str, str] = {}
        if st.ev is not None:
            j_recs = get_history_manager().get_history(st.ev)
            nicks_for_j = {r.user_id: (r.user_name or r.user_id) for r in j_recs if r.user_id and r.role == "user"}

        clean_res = sanitize_journal_summary(raw_out, max_chars=110, nicks=nicks_for_j)
        is_delivery_wake = "delivery_wake" in st.run_extra and bool(st.run_extra["delivery_wake"])
        qualifies = bool(substantive_tools) or st.image_sent_this_run or is_delivery_wake or len(clean_res) >= 60
        if not clean_res or not qualifies:
            return

        if is_delivery_wake and (not substantive_tools or substantive_tools == ["send_message_by_ai"]):
            j_tag = "交付完结"
        elif substantive_tools:
            tb0 = find_tool_base(substantive_tools[0])
            raw_t = (
                tb0.capability_domain if tb0 and tb0.capability_domain else (tb0.name if tb0 else substantive_tools[0])
            )
            j_tag = clean_journal_tag(raw_t or "日常行动")
        elif st.image_sent_this_run:
            j_tag = "图文交付"
        else:
            j_tag = "群聊答疑"

        now = datetime.now()
        gid = str(st.ev.group_id or "") if st.ev and st.ev.group_id else ""
        journal_turn = current_turn()
        journal_active = journal_turn.active_entry() if journal_turn is not None else None
        req_uid = (
            journal_active.item.user_id
            if journal_active is not None and journal_active.item.user_id
            else (str(st.ev.user_id).strip() if st.ev is not None and st.ev.user_id else "")
        )
        entry = JournalEntry(
            timestamp=now.timestamp(),
            date_str=now.strftime("%Y-%m-%d"),
            time_str=now.strftime("%H:%M"),
            tag=j_tag,
            summary=clean_res,
            group_id=gid,
            requested_by=req_uid,
        )
        asyncio.create_task(
            record_journal_entry(
                entry,
                target_personas=[self.persona_name] if self.persona_name else None,
            )
        )

    async def _note_truncated_usage(
        self,
        st: RunOnceState,
        statistics_manager: Any,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        count_run: bool,
        close_chat: bool,
    ) -> None:
        """把一段用量写入统计和会话日志。close_chat 只在整轮合计时用一次。"""
        model = st.model_name or "unknown"
        chat_type = st.stats_chat_type if st.stats_chat_type else self.create_by
        if count_run:
            nested = bool(self.is_subagent) or (bool(st.user_turn_id) and not st.owns_user_turn)
            statistics_manager.record_agent_run(
                owns_user_turn=bool(st.owns_user_turn),
                under_user_turn=bool(st.user_turn_id),
                is_nested=nested,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )
        if input_tokens > 0 or output_tokens > 0:
            statistics_manager.record_token_usage(
                model_name=model,
                chat_type=chat_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )
            if st.budget_scope is not None:
                # 记账失败不挡住收口，否则调研白跑一趟还交不出交付
                try:
                    from gsuid_core.ai_core.budget import budget_manager

                    await budget_manager.record_usage_scope(
                        st.budget_scope[0],
                        st.budget_scope[1],
                        st.budget_scope[2],
                        self.session_id,
                        input_tokens,
                        output_tokens,
                        cache_read_tokens,
                        cache_write_tokens,
                    )
                except Exception as exc:
                    logger.warning(i18n_t("log.agent.budget_fail", _be=exc))
        if close_chat:
            # 超轮数没有 End。不收起对话的话，合计用量会粘在最后一轮上。
            self._session_logger.log_node_transition("End")
        has_usage = input_tokens > 0 or output_tokens > 0 or cache_read_tokens > 0 or cache_write_tokens > 0
        if has_usage or (model and model != "unknown"):
            self._session_logger.log_token_usage(
                input_tokens,
                output_tokens,
                model,
                cache_read_tokens,
                cache_write_tokens,
            )

    async def _salvage_capability_delivery(self, st: RunOnceState, statistics_manager: Any) -> str | None:
        """有材料时收成交付交回调用方。返回 None 表示仍走可识别失败。"""
        if st.return_mode != "return":
            return None
        if st.delegated_render and not st.image_sent_this_run:
            return None
        if self.create_by not in ("CapabilityAgent", "AutoPlanner"):
            return None
        material = await self._capability_salvage_material(st)
        if not material.strip():
            return None
        question = (st.last_user_question or "完成任务描述里的交付")[:2000]
        prompt = (
            f"【任务】\n{question}\n\n{material}\n\n"
            "思考轮数已用尽，禁止调用工具。只根据上面的材料写出本任务的交付。"
            "材料里没有的事实写进缺口，禁止编造。"
            "用条目列出结论、依据和时点。"
            f"第一行必须是：{_USAGE_LIMIT_SALVAGE_BANNER}"
        )
        _fb_settings: ModelSettings | None = None
        if self.max_tokens is not None:
            _fb_settings = ModelSettings(max_tokens=int(self.max_tokens))
        _fallback_agent = Agent(
            model=self.model,
            system_prompt=self.system_prompt or "你是一个智能助手。",
            model_settings=_fb_settings,
            tools=[],
            toolsets=[],
            retries=0,
            output_type=str,
        )
        result = await _fallback_agent.run(
            prompt,
            message_history=list(self.history),
            usage_limits=UsageLimits(request_limit=1),
        )
        from pydantic_ai.run import AgentRunResult

        if isinstance(result, AgentRunResult):
            usage = result.usage
            st.usage_input_tokens += usage.input_tokens
            st.usage_output_tokens += usage.output_tokens
            st.usage_cache_read_tokens += usage.cache_read_tokens
            st.usage_cache_write_tokens += usage.cache_write_tokens
        text = str(result.output).strip()
        if not text or is_silence_marker(text):
            return None
        if not text.startswith("⚠"):
            text = f"{_USAGE_LIMIT_SALVAGE_BANNER}\n\n{text}"
        from gsuid_core.ai_core.buildin_tools.subagent import (
            looks_like_incomplete_subagent_delivery,
        )

        if looks_like_incomplete_subagent_delivery(text):
            return None
        logger.info(i18n_t("log.agent.usage_limit_salvage"))
        return text

    async def _capability_salvage_material(self, st: RunOnceState) -> str:
        """优先用本会话已落盘的检索原文（按新到旧）。没有再退回本轮摘录。"""
        persisted = await load_session_salvage_material(self.session_id or "")
        thinking = collect_run_thinking(st.thinking_segments)
        if persisted:
            body = f"【已落盘的检索原文】\n{persisted}"
        else:
            tool_outputs = "\n".join(st.run_tool_outputs)
            body = f"【本轮已查到的内容】\n{tool_outputs}" if tool_outputs else ""
        if thinking:
            clue = f"【本轮的推理线索】\n{thinking}"
            body = f"{body}\n\n{clue}" if body else clue
        return body

    async def _run_once_usage_limit_fallback(
        self,
        st: RunOnceState,
        statistics_manager: Any,
    ) -> Any:
        """UsageLimitExceeded 专属兜底总结。"""
        # 达到限制后的处理逻辑
        if st.wall_timed_out:
            budget_s = int(self.wall_clock_budget or 0)
            logger.warning(i18n_t("log.agent.wall_clock_hard_stop", p0=budget_s))
            err_type, err_msg = "wall_clock_timeout", f"达到墙钟预算硬上限 {budget_s}s"
        elif st.token_budget_exceeded:
            logger.warning(
                i18n_t(
                    "log.agent.token_budget_hard_stop",
                    p0=st.usage_input_tokens,
                    p1=st.usage_output_tokens,
                )
            )
            err_type = "token_budget_exceeded"
            err_msg = f"达到能力代理累计 Token 硬上限 in={st.usage_input_tokens} out={st.usage_output_tokens}"
        else:
            req_lim = _require_limits(st).request_limit
            logger.warning(i18n_t("log.agent.pydanticai_reached_maximum_thinking", p0=req_lim))
            err_type, err_msg = "usage_limit", f"达到最高思考轮数限制 {req_lim}"
        statistics_manager.record_error(error_type=err_type, message=err_msg)
        self._session_logger.log_error(err_type, err_msg)

        salvaged = None
        if st.return_mode == "return":
            salvaged = await self._salvage_capability_delivery(st, statistics_manager)

        # 主循环与收口 run 的合计用量统一记账与落盘，确保日志完整且不被覆盖
        await self._note_truncated_usage(
            st,
            statistics_manager,
            input_tokens=st.usage_input_tokens,
            output_tokens=st.usage_output_tokens,
            cache_read_tokens=st.usage_cache_read_tokens,
            cache_write_tokens=st.usage_cache_write_tokens,
            count_run=True,
            close_chat=True,
        )

        if st.return_mode == "return":
            if salvaged:
                self._session_logger.log_text_output(salvaged)
                self._session_logger.log_result(salvaged, st.tool_call_list)
                self._session_logger.log_run_end()
                return salvaged
            self._session_logger.log_run_end()
            return usage_limit_return_payload(
                create_by=self.create_by,
                is_subagent=self.is_subagent,
                delegated_render=st.delegated_render,
                image_sent=st.image_sent_this_run,
            )

        if st.delegated_render and not st.image_sent_this_run:
            self._remember_outbound_on_silence(st, "<SILENCE>")
            st.silence_reason = st.silence_reason or "delegated_render_unfinished"
            self._log_run_result(st, "<SILENCE>")
            return "<SILENCE>"

        # 「思考链过长…」是框架内部状态，原样发进群等于人格当场破功（生产日志出现过
        # 整条外泄）。预算耗尽对群友表现为"没来得及答"，由下面的强制总结接手。
        logger.debug(i18n_t("log.agent.chain_too_long_forced_summary", p0=_require_limits(st).request_limit))

        # 瞬时故障（超时/网络/5xx/529 等）不在此捕获，直接冒泡给 _execute_run 统一
        # 重试；download image 自愈、错误文案与统计同样收敛到 _execute_run。
        user_question = st.last_user_question or "用户之前提出的问题"

        # 证据只认**本轮**：真实回执正文 + 本轮推理。旧历史不作数——它已由
        # message_history 当上下文给出，再标成「已获取的信息」只会让总结拿旧话题作答。
        tool_outputs = "\n".join(st.run_tool_outputs)
        thinking = collect_run_thinking(st.thinking_segments)
        material = ""
        if tool_outputs:
            material += f"【本轮已查到的内容】\n{tool_outputs}"
        if thinking:
            material += f"\n\n【本轮的推理线索】\n{thinking}"

        if not material:
            # 本轮一点材料都没产出：没查、也没推理。此时让模型"按自己的知识回答"只会
            # 凭空编，那正是「内部库没你的分值」那类出戏句的产地。
            if self._no_material_reply_expected(st):
                return await self._deliver_no_material_reply(st, user_question)
            logger.debug(i18n_t("log.agent.chain_too_long_no_context_silence"))
            self._remember_outbound_on_silence(st, "<SILENCE>")
            st.silence_reason = st.silence_reason or "usage_limit_no_material"
            self._log_run_result(st, "<SILENCE>")
            return "<SILENCE>"

        final_message = (
            f"【用户的问题】\n{user_question}\n\n{material}\n\n"
            "请只根据以上材料回答用户的问题，人设风格不变。材料里没有的就说没有，"
            "禁止补充任何未在材料中出现的信息。禁止调用任何工具，只输出自然语言文本。"
        )

        # 创建无工具精简 Agent（tools=[] = 无 schema，从根源消除工具调用）
        from pydantic_ai.settings import ModelSettings

        _fb_settings: ModelSettings | None = None
        if self.max_tokens is not None:
            _fb_settings = ModelSettings(max_tokens=int(self.max_tokens))
        _fallback_agent = Agent(
            model=self.model,
            system_prompt=self.system_prompt or "你是一个智能助手。",
            model_settings=_fb_settings,
            tools=[],
            toolsets=[],
            retries=0,
            output_type=str,
        )

        # 带上真实对话历史：只给 final_message 等于让总结 agent 拿着一条没有群
        # 上下文的孤立消息发言，它既不知道在跟谁说话也不知道上一句指代什么。
        _fb_history: List[ModelMessage] = list(self.history)
        fallback_result = await _fallback_agent.run(
            final_message,
            message_history=_fb_history,
            usage_limits=UsageLimits(request_limit=1),
        )

        # 强制总结同样是一次真实 LLM 往返，把它的最终产出记进当前 session
        # logger（与本 run 同一文件）——否则"超轮数兜底"答复在日志里不可见。
        fallback_text = str(fallback_result.output)
        if not fallback_text.strip() or is_silence_marker(fallback_text.strip()):
            self._remember_outbound_on_silence(st, "<SILENCE>")
            self._session_logger.log_result(
                "<SILENCE>",
                st.tool_call_list,
                silence_reason=st.silence_reason or "usage_limit_fallback_silence",
            )
            return "<SILENCE>"
        self._session_logger.log_text_output(fallback_text)
        self._session_logger.log_result(fallback_text, st.tool_call_list)

        if st.bot:
            await send_chat_result(st.bot, fallback_result.output, ev=st.ev, extra_metadata=turn_reply_metadata(st.ev))
            absorb_run_image_delivered(st)
        return ""

    def _no_material_reply_expected(self, st: RunOnceState) -> bool:
        """本轮无材料时，用户**明确在等**回答吗（私聊 / @点名 / 省略续聊）。

        旁观轮与私聊不能同等对待：未寻址轮本来就该沉默，而私聊里对方问了一句话却
        收到零输出，是把「没答上来」误装成「不想理」。
        """
        if st.ev is not None and not st.ev.group_id:
            return True
        tg = st.tg
        if tg is None:
            return False
        return bool(tg.call_to_self or tg.ellipsis_followup)

    async def _deliver_no_material_reply(self, st: RunOnceState, user_question: str) -> str:
        """无材料但对方在等：给一句角色口吻的「没答上来」，不解释、不编造。"""
        from pydantic_ai.settings import ModelSettings

        _fb_settings: ModelSettings | None = None
        if self.max_tokens is not None:
            _fb_settings = ModelSettings(max_tokens=int(self.max_tokens))
        _agent = Agent(
            model=self.model,
            system_prompt=self.system_prompt or "你是一个智能助手。",
            model_settings=_fb_settings,
            tools=[],
            toolsets=[],
            retries=0,
            output_type=str,
        )
        _msg = (
            f"【用户的问题】\n{user_question}\n\n"
            "你没查到任何材料，也还没想出结论。用人设口吻承认这次没答上来，"
            "一句话、20 字以内。禁止编造内容，禁止解释为什么没查到，"
            "禁止提到检索、工具、数据库一类的东西。"
        )
        result = await _agent.run(
            _msg,
            message_history=list(self.history),
            usage_limits=UsageLimits(request_limit=1),
        )
        text = str(result.output)
        if not text.strip() or is_silence_marker(text.strip()):
            self._remember_outbound_on_silence(st, "<SILENCE>")
            self._session_logger.log_result(
                "<SILENCE>",
                st.tool_call_list,
                silence_reason=st.silence_reason or "no_material_reply_silence",
            )
            return "<SILENCE>"
        self._session_logger.log_text_output(text)
        self._session_logger.log_result(text, st.tool_call_list)
        if st.bot:
            await send_chat_result(st.bot, text, ev=st.ev, extra_metadata=turn_reply_metadata(st.ev))
            absorb_run_image_delivered(st)
        return ""

    def _attach_unfinished_tool_return(self, st: RunOnceState, result_msg: str) -> str:
        """能力代理没写成结论时，把最后一次工具返回交回调用方。"""
        from gsuid_core.ai_core.agent_run.support import _UNFINISHED_CAPABILITY_BANNER

        text = str(result_msg).strip()
        if self.create_by != "CapabilityAgent" or st.return_mode != "return":
            return result_msg
        if text and not is_silence_marker(text):
            return result_msg
        last = ""
        for chunk in reversed(st.run_tool_outputs):
            piece = chunk.strip()
            if piece:
                last = piece
                break
        if not last:
            return result_msg
        return f"{_UNFINISHED_CAPABILITY_BANNER}\n{last[:12000]}"

    def _log_run_result(self, st: RunOnceState, result_msg: str) -> None:
        """沉默或空输出时把原因写进 result。有正文则不带 silence_reason。"""
        from gsuid_core.ai_core.agent_run.support import result_silence_reason

        reason = result_silence_reason(
            str(result_msg),
            existing=st.silence_reason,
            blocked=st.last_block_reason,
        )
        self._session_logger.log_result(result_msg, st.tool_call_list, silence_reason=reason)

    def _remember_outbound_on_silence(self, st: RunOnceState, result_msg: str) -> None:
        """静默不进 A 轨；把本轮出站句柄补上，追问才能 read_handle。"""
        if self.is_subagent or self.create_by not in ("Chat", "Agent"):
            return
        if not result_msg or not is_silence_marker(result_msg.strip()):
            return
        ev = st.ev
        if ev is None or not ev.group_id:
            return
        from gsuid_core.ai_core.history_format import noted_thread_handles, remember_silence_handles

        remember_silence_handles(ev, noted_thread_handles(st.context))

    def _run_once_cleanup(self, st: RunOnceState) -> None:
        """finally：还原 budget scope / 墙钟 / 单轮节流。"""
        # 纠正轮结束时父 settle 还要读这些；必须在 st 丢弃前写到宿主。
        self._last_attempt_delegated_render = st.delegated_render
        self._last_attempt_image_sent = st.image_sent_this_run
        self._last_attempt_pending_async = st.pending_async_delivery
        self._last_attempt_effectual_mutate = st.effectual_mutate
        self._last_attempt_has_status_tool = st.has_status_tool_call
        self._last_attempt_tool_outcomes = (frozenset(st.succeeded_tools), frozenset(st.failed_tools))
        from gsuid_core.ai_core.const import THINKING_TEXT_MAX

        thinking_blob = "\n".join(s for s in st.thinking_segments if s)
        self._last_attempt_thinking = thinking_blob[-THINKING_TEXT_MAX:] if thinking_blob else ""
        # 还原预算 scope contextvar，避免本次绑定泄漏到上层调用栈。
        if st.budget_scope_token is not None:
            _current_budget_scope.reset(st.budget_scope_token)
        # 还原墙钟上下文（嵌套 run 不破坏父 run 时钟）
        if st.wall_token is not None:
            wall_clock.uninstall_clock(st.wall_token)
            st.wall_token = None
        # 仅 root 主人格绑定了 user_turn contextvar；嵌套 run 不 reset，避免提前清空父树。
        if st.user_turn_token is not None:
            reset_user_turn_id(st.user_turn_token)
            st.user_turn_token = None
        # 清理本轮的单轮节流计数（scheduler.py schedule_task 等共享）， 防止内存中 key 无限累积。
        try:
            from gsuid_core.ai_core.buildin_tools.scheduler import (
                clear_turn_throttle,
            )
            from gsuid_core.ai_core.buildin_tools.message_sender import (
                clear_turn_send_throttle,
            )

            sess = st.ev.session_id if st.ev is not None else None
            if sess:
                clear_turn_throttle(str(sess), st.turn_id)
                clear_turn_send_throttle(str(sess), st.turn_id)
        except Exception as _e:
            logger.debug(i18n_t("log.agent.clear_counter", _e=_e))
