"""阶段 D：收尾（history/闸门/假完成）+ UsageLimit 兜底 + finally 清理"""

from __future__ import annotations

import re
import time
from typing import Any, List, Sequence

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
from gsuid_core.ai_core import (
    wall_clock,
    output_gate,
    output_firewall,
)
from gsuid_core.ai_core.const import (
    _STICKY_FAMILY_TURNS,
    _PROGRESSIVE_TOOLS_SKIP_INTENTS,
)
from gsuid_core.ai_core.utils import (
    NO_RESULT_TEXT,
    send_chat_result,
    _relean_user_turn,
    is_silence_marker,
    _extract_run_context,
)
from gsuid_core.ai_core.register import find_tool_base
from gsuid_core.ai_core.agent_run.host import RunOnceHost
from gsuid_core.ai_core.agent_run.state import (
    RunOnceState,
    _require_limits,
    _require_context,
)
from gsuid_core.ai_core.agent_run.support import (
    _WALL_CLOCK_NUDGE,
    _RENDER_TOOL_NAMES,
    _THRASH_FUSE_NUDGE,
    _WALL_CLOCK_PIPELINE,
    _INTERACTIVE_CREATE_BY,
    _claims_fake_done,
    _correction_nudge_markers,
    _looks_like_report_speech,
)
from gsuid_core.ai_core.control.directive import (
    Directive,
    obligation_satisfied,
    render_control_envelope,
)
from gsuid_core.ai_core.control.corrections import (
    fake_done_directive,
    status_zero_tool_directive,
    render_obligation_directive,
    structural_zero_tool_directive,
)
from gsuid_core.ai_core.agent_run.budget_ctx import _current_budget_scope
from gsuid_core.ai_core.agent_run.speech_policy import (
    looks_like_process_meta,
    looks_like_wait_comfort,
    looks_like_empty_handoff,
    strip_open_solicitations,
    claims_premature_delivery,
    has_orchestration_narration,
    looks_like_numeric_recitation,
)
from gsuid_core.ai_core.agent_run.user_turn_ctx import reset_user_turn_id


async def _deliver_withheld(st: RunOnceState, sent: set[str]) -> None:
    """把排版闸暂扣的原文真正发给用户（INV-4 兜底，只发第一段防刷屏）。"""
    for body in st.presentation_withheld:
        if not body or body in sent:
            continue
        if st.bot is None:
            return
        await send_chat_result(st.bot, body, ev=st.ev)
        sent.add(body)
        return


def _satisfaction_facts(st: RunOnceState) -> tuple[str, ...]:
    """把 RunOnceState 投影成义务履行判据（结构事实，不看模型文本）。"""
    facts: list[str] = []
    if st.image_sent_this_run:
        facts.append("image_sent")
    if st.delegated_render:
        facts.append("render_delegated")
    if st.has_status_tool_call:
        facts.append("status_tool_called")
    if st.tool_call_list:
        facts.append("any_tool_called")
    if "check_delegation" in st.tool_call_list:
        facts.append("delegation_checked")
    return tuple(facts)


def _obligations_met(directive: Directive, st: RunOnceState) -> bool:
    """指令的全部义务是否已结构化履行（INV-B）。"""
    facts = _satisfaction_facts(st)
    return all(obligation_satisfied(ob, facts=facts, tool_calls=st.tool_call_list) for ob in directive.obligations)


def _absorb_attempt_facts(
    st: RunOnceState,
    *,
    tool_calls: Sequence[str],
    delegated_render: bool,
    image_sent: bool,
    pending_async: bool,
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
    if has_status_tool:
        st.has_status_tool_call = True


_RENDER_OBLIGATION_REASONS: frozenset[str] = frozenset({"report_speech", "empty_handoff", "numeric_recitation"})


def _has_unread_attachment(st: RunOnceState) -> bool:
    """本条是否带未读附件（图/音频/文件）。只看 Event 结构字段。"""
    ev = st.ev
    if ev is None:
        return False
    if ev.image_id_list or ev.image_id:
        return True
    if ev.audio_id_list or ev.audio_id:
        return True
    return bool(ev.file)


def _zero_tool_needs_correction(st: RunOnceState) -> bool:
    """零工具纠正只认正证据：未读附件或可继承的上轮工具任务。"""
    if _has_unread_attachment(st):
        return True
    if st.followup_detected:
        return True
    return bool(st.tg is not None and st.tg.ellipsis_followup)


def _needs_render_obligation(st: RunOnceState, result_msg: str) -> bool:
    """有出处凭据且台词呈报告体 / 空交付暂扣时才进纠正。mismatch 单独不够。"""
    if not st.saw_structured_return or not st.tool_call_list:
        return False
    if _looks_like_report_speech(result_msg or ""):
        return True
    if looks_like_empty_handoff(result_msg or ""):
        return True
    if looks_like_numeric_recitation(result_msg or ""):
        return True
    return any(r in _RENDER_OBLIGATION_REASONS for r in st.presentation_withheld_reasons)


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


def _correction_is_deliverable(text: str) -> bool:
    """纠正产出是否可直接交付（非沉默、非编排/元叙述脏输出）。"""
    body = (text or "").strip()
    if not body or is_silence_marker(body):
        return False
    return not (
        has_orchestration_narration(body)
        or claims_premature_delivery(body)
        or _looks_like_report_speech(body)
        or looks_like_process_meta(body)
    )


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


def _corrected_or_original(corrected: object, *, original: str) -> str:
    """纠正轮结果收敛（INV-3）。

    纠正**只有**产出可交付内容才有权替换原答案；沉默或脏输出一律保留原答案，
    否则「纠正判据误报 → 模型沉默 → 原答案被销毁」会让整轮对用户零输出。
    """
    if not isinstance(corrected, str) or not _correction_is_deliverable(corrected):
        return original
    return strip_open_solicitations(corrected.strip()) or original


class SettlePhase(RunOnceHost):
    def _record_prefix_break_probe(self, st: RunOnceState, new_msgs: List[ModelMessage]) -> None:
        """对比上一 run 发送快照与当前 history 头，记 prefix_break_reason。"""
        from gsuid_core.ai_core.prefix_probe import (
            PrefixSnapshot,
            tools_diff as _tools_diff,
            hash_tool_names,
            history_payloads,
            hash_system_prompt,
            record_prefix_break,
            classify_prefix_break,
            hash_history_messages,
        )

        tools_hash = hash_tool_names(st.tool_names)
        system_hash = hash_system_prompt(self.system_prompt or "")
        hist_hashes = hash_history_messages(self.history)
        prev = self._prefix_snapshot
        reason = classify_prefix_break(
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
        record_prefix_break(reason)
        self._session_logger.log_prefix_break(reason, tools_hash=tools_hash, system_hash=system_hash, tools_diff=diff)
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
        # 遍历完成后，直接从 agent_run 中获取最终结果
        result = agent_run.result
        if result:
            logger.info(i18n_t("log.agent.iter_ok"))

            # 存 history 前把本轮 user turn 的 content 换成精简版（剥离 st.rag_context）
            # 防止 [历史对话]/记忆/群语境快照逐轮累积膨胀 input 并冲淡缓存（§优化 O-1）。
            _new_msgs = result.new_messages()
            # 只剥框架注入；入史 == 最后一次请求所见（前缀缓存）。
            _relean_user_turn(
                _new_msgs,
                st.lean_user_message,
                strip_hint_texts=(
                    _WALL_CLOCK_NUDGE,
                    _WALL_CLOCK_PIPELINE,
                    _THRASH_FUSE_NUDGE,
                    *output_gate.GATE_NUDGE_MARKERS,
                    *_correction_nudge_markers(),
                ),
            )
            _new_msgs = [m for m in _new_msgs if not (isinstance(m, ModelRequest) and len(m.parts) == 0)]
            _new_msgs = _dedupe_delivery_cards(_new_msgs)
            _new_msgs = _drop_unsent_text_from_tail(_new_msgs, st.unsent_texts)
            self._record_prefix_break_probe(st, _new_msgs)
            self.history.extend(_new_msgs)
            _ctx_dyn = st.context
            if _ctx_dyn is not None:
                for _dn in _ctx_dyn.dynamic_tool_names:
                    if _dn and _dn not in self._session_appended_tools:
                        self._session_appended_tools.append(_dn)

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
                else:
                    self._consecutive_no_tool_rounds += 1
                    # 意图-行为不一致检测（结构化）：thinking 里提到了本轮
                    # 已装配的工具名却没真正调用——顶到阈值，下轮强制提醒。
                    thinking_blob = "\n".join(st.thinking_segments)
                    if thinking_blob and st.tool_names and any(tn in thinking_blob for tn in st.tool_names):
                        self._consecutive_no_tool_rounds = max(self._consecutive_no_tool_rounds, 2)
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

                # 小时级性能统计（TTFT/TPS）已在每轮 CallToolsNode 中按请求结算,
                # 此处记录 run 级 Token 汇总 + User Turn / Agent Run 效率计数。
                # 计数在 token 为 0 时仍记（完整 settle 的 run 也算一次），避免漏计分母。
                _is_nested = bool(self.is_subagent) or (bool(st.user_turn_id) and not st.owns_user_turn)
                if input_tokens > 0 or output_tokens > 0:
                    statistics_manager.record_token_usage(
                        model_name=st.model_name,
                        chat_type=self.create_by,
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
            # 工具调用列表只进调试日志，不追加到用户可见消息
            if st.tool_call_list:
                logger.debug(i18n_t("log.agent.current_tool_call_event", p0=", ".join(st.tool_call_list)))

            self._session_logger.log_run_end()
            # result 在出口消毒后再记，避免 raw 念数被当成已出站。

            # 假完成结算（结构判据收口）。同轮至多一次纠正重跑，防 status+render 双触发。
            _settle_correction_ran = False

            async def _resend_fab_blocked() -> None:
                for _bt in st.fab_blocked:
                    if _bt in self._run_sent_texts:
                        continue
                    try:
                        if st.bot is None:
                            logger.warning(i18n_t("log.agent.fakedone_bot_object_unavailable"))
                            continue
                        await send_chat_result(st.bot, _bt, ev=st.ev)
                        self._run_sent_texts.add(_bt)
                    except Exception as _se:
                        logger.debug(i18n_t("log.agent.fakedone_se", _se=_se))

            if st.fab_blocked and st.tool_call_list and st.bot and st.return_mode in ["always", "by_bot"]:
                logger.info(i18n_t("log.agent.fakedone_claim"))
                await _resend_fab_blocked()
            elif (
                result_msg
                and not st.tool_call_list
                and st.tool_names
                and not st.fake_done_retry
                # 结构证据：预检暂扣 or 文本宣称完成；不靠 st.intent 标签（误标会误伤闲聊）
                and (st.fab_blocked or _claims_fake_done(result_msg))
            ):
                _settle_correction_ran = True
                logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
                try:
                    corrected = await self._execute_run_once(
                        user_message=render_control_envelope((fake_done_directive(tool_pool_size=len(st.tool_names)),)),
                        bot=st.bot,
                        ev=st.ev,
                        tools=st.tools,
                        return_mode=st.return_mode,
                        intent=st.intent,
                        has_active_task=st.has_active_task,
                        suppress_intermediate_text=st.suppress_intermediate_text,
                        fake_done_retry=True,
                        is_framework_injection=True,
                    )
                except Exception as _fe:
                    # 纠正 pass 是增强路径，失败不影响原结果返回；暂扣文本补发防"整轮沉默"
                    logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_fe))
                    corrected = None
                    if st.fab_blocked and st.bot and st.return_mode in ["always", "by_bot"]:
                        await _resend_fab_blocked()
                # 与其它三条纠正的 INV-3 有意分岔：原答案是**编造的完成声明**，
                # 不能当 fallback 留给用户；无干净纠正则静默，并一律剥掉那句谎话。
                _fabricated = {t.strip() for t in st.fab_blocked}
                if _claims_fake_done(result_msg):
                    _fabricated.add(result_msg.strip())
                if isinstance(corrected, str) and _correction_is_deliverable(corrected):
                    result_msg = strip_open_solicitations(corrected.strip()) or "<SILENCE>"
                else:
                    result_msg = "<SILENCE>"
                self._scrub_fake_done_history(_fabricated)

            # 结构假完成：未读附件或可继承跟进 + 池非空 + 零调用。无正证据不开。
            elif (
                result_msg
                and not st.tool_call_list
                and st.tool_names
                and not st.fake_done_retry
                and self.create_by in _INTERACTIVE_CREATE_BY
                and self.create_by != "CapabilityAgent"
                and st.ev is not None
                and _zero_tool_needs_correction(st)
                and not is_silence_marker(result_msg.strip())
            ):
                _settle_correction_ran = True
                logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
                try:
                    corrected = await self._execute_run_once(
                        user_message=render_control_envelope(
                            (structural_zero_tool_directive(tool_pool_size=len(st.tool_names)),)
                        ),
                        bot=st.bot,
                        ev=st.ev,
                        tools=st.tools,
                        return_mode=st.return_mode,
                        intent=st.intent,
                        has_active_task=st.has_active_task,
                        suppress_intermediate_text=st.suppress_intermediate_text,
                        fake_done_retry=True,
                        is_framework_injection=True,
                    )
                except Exception as _fe:
                    logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_fe))
                    corrected = None
                # 自洽出口（INV-3）：纠正沉默 → 原答案生效；只有真产出才替换并剥旧答
                _prior = result_msg.strip()
                result_msg = _corrected_or_original(corrected, original=result_msg)
                if result_msg.strip() != _prior:
                    self._scrub_fake_done_history({_prior} if _prior else set())
                else:
                    self._scrub_fake_done_history(set())

            # 进度追问却零工具：纠正重跑去查 kanban/artifact
            elif (
                st.status_inquiry
                and st.has_active_task
                and not st.tool_call_list
                and not st.fake_done_retry
                and result_msg
                and not is_silence_marker(result_msg.strip())
                and self.create_by in ("Chat", "Agent")
            ):
                _settle_correction_ran = True
                logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
                try:
                    _sc = await self._execute_run_once(
                        user_message=render_control_envelope((status_zero_tool_directive(),)),
                        bot=st.bot,
                        ev=st.ev,
                        tools=st.tools,
                        return_mode=st.return_mode,
                        intent=st.intent,
                        has_active_task=st.has_active_task,
                        suppress_intermediate_text=st.suppress_intermediate_text,
                        fake_done_retry=True,
                        is_framework_injection=True,
                    )
                except Exception as _se:
                    logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_se))
                    _sc = None
                result_msg = _corrected_or_original(_sc, original=result_msg)
                self._scrub_fake_done_history(set())

            # 申辩/义务未履行时，出口消毒不得再按报告体静默（否则暂扣原文永远发不出）
            _skip_report_exit = False
            _replacement_visible = False
            # 出处凭据 + 尚未出图。排版失配只决定要不要进纠正，不单独构成义务。
            _render_obligation = _needs_render_obligation(st, result_msg)
            if (
                not _settle_correction_ran
                and _render_obligation
                and not st.delegated_render
                and not st.pending_async_delivery
                and not st.image_sent_this_run
                and not (_RENDER_TOOL_NAMES & set(st.tool_call_list))
                and not st.fake_done_retry
                and self.create_by in _INTERACTIVE_CREATE_BY
                and self.create_by != "CapabilityAgent"
            ):
                logger.warning(i18n_t("log.agent.render_data_nudge_once"))
                _directive = render_obligation_directive(
                    recited_report=_looks_like_report_speech(result_msg or ""),
                    tool_calls=len(st.tool_call_list),
                )
                _disputes_before = len(self._run_disputes)
                _sent_before_correction = set(self._run_sent_texts)
                try:
                    _rc = await self._execute_run_once(
                        user_message=render_control_envelope((_directive,)),
                        bot=st.bot,
                        ev=st.ev,
                        tools=st.tools,
                        return_mode=st.return_mode,
                        intent=st.intent,
                        has_active_task=st.has_active_task,
                        suppress_intermediate_text=True,
                        fake_done_retry=True,
                        is_framework_injection=True,
                    )
                except Exception as _re:
                    logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=_re))
                    _rc = None
                # 纠正轮是新 st；先并回父级再判义务，否则嵌套 create_subagent 恒未履行
                _absorb_attempt_facts(
                    st,
                    tool_calls=self._last_attempt_tool_calls,
                    delegated_render=self._last_attempt_delegated_render,
                    image_sent=self._last_attempt_image_sent,
                    pending_async=self._last_attempt_pending_async,
                    has_status_tool=self._last_attempt_has_status_tool,
                )
                _disputed = len(self._run_disputes) > _disputes_before
                _orig_before = result_msg
                # 模型申辩了观察不成立 → 原答案照原样交付（这正是它不再对用户反驳的前提）
                if _disputed:
                    logger.info(i18n_t("log.agent.directive_disputed", reason=self._run_disputes[-1][:120]))
                    self._scrub_fake_done_history(set())
                else:
                    # INV-3：纠正未产出可交付内容 → 原答案生效，绝不因纠正沉默吞掉本轮
                    result_msg = _corrected_or_original(_rc, original=result_msg)
                    _drop_texts: set[str] = set()
                    if result_msg.strip() != _orig_before.strip():
                        _drop_texts.add(_orig_before.strip())
                    self._scrub_fake_done_history(_drop_texts)
                    if not _obligations_met(_directive, st):
                        logger.info(i18n_t("log.agent.directive_obligation_unmet", code=_directive.reason_code))
                # 申辩或义务仍未履行 → 出口消毒不得再按报告体静默（否则暂扣原文发不出）
                _skip_report_exit = _disputed or not _obligations_met(_directive, st)
                # 已有替代品时不得再冲刷暂扣原文；等一句安抚不算替代
                _adopted = (not _disputed) and result_msg.strip() != _orig_before.strip()
                _nested_visible = any(
                    t.strip() and t not in st.presentation_withheld and not looks_like_wait_comfort(t)
                    for t in (self._run_sent_texts - _sent_before_correction)
                )
                _replacement_visible = _adopted or _nested_visible

            # 出口消毒：异步在途 / 编排泄漏 / 长结构 / 引导追问 → 对外 SILENCE 或短句
            if self.create_by in ("Chat", "Agent") and result_msg:
                _rs = result_msg.strip()
                if st.image_sent_this_run:
                    # 步骤 7：发图后允许短收尾；仍砍编排/长结构/过程元话语/引导追问
                    if (
                        has_orchestration_narration(_rs)
                        or _looks_like_report_speech(_rs)
                        or looks_like_process_meta(_rs)
                    ):
                        result_msg = "<SILENCE>"
                    else:
                        _stripped = strip_open_solicitations(_rs)
                        if _stripped != _rs:
                            result_msg = _stripped if _stripped else "<SILENCE>"
                        elif len(_rs) > 120:
                            result_msg = "<SILENCE>"
                elif (st.pending_async_delivery or st.delegated_render) and not is_silence_marker(_rs):
                    # 在途默认静默；极短等待安慰可保留一次
                    if looks_like_wait_comfort(_rs) and not st.wait_comfort_sent:
                        result_msg = _rs
                    else:
                        result_msg = "<SILENCE>"
                elif has_orchestration_narration(_rs) or looks_like_process_meta(_rs):
                    result_msg = "<SILENCE>"
                elif claims_premature_delivery(_rs) and not st.image_sent_this_run:
                    result_msg = "<SILENCE>"
                elif looks_like_empty_handoff(_rs) and not st.image_sent_this_run:
                    result_msg = "<SILENCE>"
                elif not st.image_sent_this_run and looks_like_numeric_recitation(_rs):
                    result_msg = "<SILENCE>"
                elif (
                    _render_obligation
                    and not _skip_report_exit
                    and not st.image_sent_this_run
                    and _looks_like_report_speech(_rs)
                ):
                    # 有真事实包却念表 → 静默（该走 render）；无事实包的长正文放行
                    result_msg = "<SILENCE>"
                else:
                    _stripped = strip_open_solicitations(_rs)
                    if _stripped != _rs:
                        result_msg = _stripped if _stripped else "<SILENCE>"

            # INV-4：暂扣原文在纠正未兑现（或根本没进纠正）且没有替代品时必须发出。
            if _should_deliver_withheld(
                st,
                skip_report_exit=_skip_report_exit,
                replacement_visible=_replacement_visible,
            ):
                await _deliver_withheld(st, self._run_sent_texts)

            self._session_logger.log_result(result_msg, st.tool_call_list)
            if st.return_mode in ["by_bot"] and st.bot and st.ev:
                return ""
            # 对用户可见出口才做 roleplay OOC；子代理/能力代理 return 必须保留 res_ 句柄
            if result_msg and output_firewall.is_enabled():
                _skip_roleplay_scrub = self.is_subagent or self.create_by in (
                    "CapabilityAgent",
                    "AutoPlanner",
                )
                if _skip_roleplay_scrub:
                    if output_firewall.is_tech_dump(result_msg):
                        result_msg = "⚠️ 子任务返回技术错误堆栈，已屏蔽。请主人格换路或重试（勿向用户念本句）。"
                else:
                    result_msg, _ooc_scrubbed = output_firewall.scrub_or_fallback(
                        result_msg,
                        user_text=st.ev.raw_text if st.ev is not None and st.ev.raw_text else "",
                    )
                    if _ooc_scrubbed:
                        logger.warning(i18n_t("log.agent.firewall_run_return_value_hit"))
            return result_msg

        # result 为空时的默认返回值（常量：handle_ai 好感度门等消费端按它识别准失败轮）
        return NO_RESULT_TEXT

    async def _run_once_usage_limit_fallback(
        self,
        st: RunOnceState,
        statistics_manager: Any,
    ) -> Any:
        """UsageLimitExceeded 专属兜底总结。"""
        # 达到限制后的处理逻辑
        logger.warning(i18n_t("log.agent.pydanticai_reached_maximum_thinking", p0=_require_limits(st).request_limit))
        statistics_manager.record_error(error_type="usage_limit")
        self._session_logger.log_error("usage_limit", f"达到最高思考轮数限制 {_require_limits(st).request_limit}")

        # 子代理（return 模式，如 Kanban 能力代理 / plugin_developer_agent）： **绝不**直接对用户的 st.bot 说话
        # 也**绝不**把超轮数的中间产物强制总结后回灌
        if st.return_mode == "return":
            # 出图在途：内部轮数耗尽不对用户念框架错误
            if st.delegated_render and not st.image_sent_this_run:
                return "<SILENCE>"
            return (
                "⚠️ 已达最大思考轮数，未能在限定步数内完成本任务。"
                "中间产物（如已写入的文件 / artifact）已留在工作区，未回传以避免刷屏。"
            )

        if st.delegated_render and not st.image_sent_this_run:
            return "<SILENCE>"

        # 安抚用户
        if st.bot:
            await st.bot.send(await st.bot.t("log.ai_agent.chain_too_long_summary"))

        # ✨ 【关键点2】发起"强制总结"请求
        try:
            user_question = st.last_user_question or "用户之前提出的问题"

            # 从历史中提取已获取的事实和模型推理片段
            run_context = _extract_run_context(self.history)

            if run_context:
                final_message = (
                    f"【用户的问题】\n{user_question}\n\n"
                    f"【已获取的信息和推理过程】\n{run_context}\n\n"
                    "请根据以上已知信息，根据人设风格直接回答用户的问题。"
                    "禁止调用任何工具，只输出自然语言文本。"
                )
            else:
                final_message = (
                    f"【用户的问题】\n{user_question}\n\n"
                    "请直接回答这个问题（根据你的已有知识和角色性格），不要调用任何工具。"
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

            # message_history 为空：所有上下文已聚焦到 final_message 中
            fallback_result = await _fallback_agent.run(
                final_message,
                message_history=[],
                usage_limits=UsageLimits(request_limit=1),
            )

            # 强制总结同样是一次真实 LLM 往返，把它的最终产出记进当前 session
            # logger（与本 run 同一文件）——否则"超轮数兜底"答复在日志里不可见。
            fallback_text = str(fallback_result.output)
            self._session_logger.log_text_output(fallback_text)
            self._session_logger.log_result(fallback_text, st.tool_call_list)

            if st.bot:
                await send_chat_result(st.bot, fallback_result.output, ev=st.ev)
            return ""

        except Exception as e:
            logger.error(i18n_t("log.agent.pydanticai_forced_summary", e=e))
            self._session_logger.log_error("fallback_failed", str(e))
            fallback_error = "⚠️ 问题较复杂，现有信息不足以给出准确答案。可以尝试提高思维链长度，或换个方式描述问题。"
            if st.bot:
                await st.bot.send(fallback_error)
                return ""
            return fallback_error

            # 瞬时故障（超时/网络/5xx/529 等）一律不在此捕获，向上抛给 _execute_run
            # 统一重试；download image 自愈与错误文案/统计也收敛到 _execute_run。

    def _run_once_cleanup(self, st: RunOnceState) -> None:
        """finally：还原 budget scope / 墙钟 / 单轮节流。"""
        # 纠正轮结束时父 settle 还要读这些；必须在 st 丢弃前写到宿主。
        self._last_attempt_delegated_render = st.delegated_render
        self._last_attempt_image_sent = st.image_sent_this_run
        self._last_attempt_pending_async = st.pending_async_delivery
        self._last_attempt_has_status_tool = st.has_status_tool_call
        thinking_blob = "\n".join(s for s in st.thinking_segments if s)
        from gsuid_core.ai_core.configs.ai_config import ai_config as _ai_cfg

        _think_max = int(_ai_cfg.get_config("thinking_text_max").data)
        self._last_attempt_thinking = thinking_blob[-_think_max:] if thinking_blob else ""
        # 还原预算 scope contextvar，避免本次绑定泄漏到上层调用栈。
        if st.budget_scope_token is not None:
            _current_budget_scope.reset(st.budget_scope_token)
        # 仅 root 主人格绑定了 user_turn contextvar；嵌套 run 不 reset，避免提前清空父树。
        if st.user_turn_token is not None:
            reset_user_turn_id(st.user_turn_token)
            st.user_turn_token = None
        # 同理还原墙钟时钟：嵌套 run 结束后父 run 必须拿回自己的累加器。
        if st.wall_clock_token is not None:
            wall_clock.uninstall_clock(st.wall_clock_token)
        # 清理本轮的单轮节流计数（scheduler.py add_once_task 等共享）， 防止内存中 key 无限累积。
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
