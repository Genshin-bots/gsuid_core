"""阶段 D：收尾（history/闸门/假完成）+ UsageLimit 兜底 + finally 清理"""

from __future__ import annotations

import re
import time
import asyncio
from typing import Any, List, Sequence
from datetime import datetime
from dataclasses import dataclass

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
from gsuid_core.ai_core.delivery import send_chat_result, extract_pure_meme_query
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
    _MAIN_PERSONA_CREATE_BY,
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
from gsuid_core.ai_core.control.angle_guard import strip_protocol_tags
from gsuid_core.ai_core.control.policy_ring import (
    SettleRingHost,
    SettleRingInput,
    turn_user_text,
    corrected_or_original,
    run_settle_policy_ring,
    pure_meme_no_match_directive,
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
        st.main_channel_sends += 1
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
    if st.image_sent_this_run or st.delegated_render or st.blocks_user_speech:
        return False
    if skip_report_exit:
        return True
    # 未进纠正（短 empty_handoff）时不能只靠 _skip_report_exit，否则整轮零输出
    return bool(st.saw_structured_return)


_DLG_ROOT_RE = re.compile(r"dlg_([0-9a-fA-F-]{8,})")


def _dlg_root_of(msg: object) -> str:
    """UserPromptPart 里的 dlg_ 句柄 root；无则空串。"""
    from pydantic_ai.messages import ModelRequest as _ModelRequest, UserPromptPart

    if not isinstance(msg, _ModelRequest):
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
    from gsuid_core.ai_core.planning.tool_output_store import AIToolOutputRecord, load_payload_text

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


async def _try_correction_pass_for(
    host: RunOnceHost,
    st: RunOnceState,
    directives: tuple[Directive, ...],
    *,
    suppress_intermediate_text: bool | None = None,
    return_mode: ReturnMode | None = None,
) -> object:
    """纠正重跑；失败返回 None，原答案按 INV-3 生效。成功时并回父级工具事实。"""
    suppress = st.suppress_intermediate_text if suppress_intermediate_text is None else suppress_intermediate_text
    mode = st.return_mode if return_mode is None else return_mode
    try:
        corrected = await host._execute_run_once(
            user_message=render_control_envelope(directives),
            bot=st.bot,
            ev=st.ev,
            tools=st.tools,
            return_mode=mode,
            intent=st.intent,
            has_active_task=st.has_active_task,
            suppress_intermediate_text=suppress,
            fake_done_retry=True,
            is_framework_injection=True,
            injection_reason=",".join(d.reason_code for d in directives if d.reason_code),
        )
    except Exception as fe:
        logger.warning(i18n_t("log.agent.fakedone_correction_run_keeping_fail", _fe=fe))
        host._last_attempt_tool_calls = st.tool_call_list
        return None
    _absorb_attempt_facts(
        st,
        tool_calls=host._last_attempt_tool_calls,
        delegated_render=host._last_attempt_delegated_render,
        image_sent=host._last_attempt_image_sent,
        pending_async=host._last_attempt_pending_async,
        effectual_mutate=host._last_attempt_effectual_mutate,
        has_status_tool=host._last_attempt_has_status_tool,
    )
    host._last_attempt_tool_calls = st.tool_call_list
    return corrected


async def _emit_unsent_reply_for(host: RunOnceHost, st: RunOnceState, text: str) -> None:
    """纠正拿回的句子由本层发出。return 模式不会在内层再过话术闸。"""
    body = text.strip()
    if not body or is_silence_marker(body) or body in host._run_sent_texts:
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
    host._run_sent_texts.add(body)
    if body not in host._run_sent_order:
        host._run_sent_order.append(body)
    if st.main_channel_sends == 0:
        st.main_channel_sends = 1


async def _run_inv3_correction_for(
    host: RunOnceHost,
    st: RunOnceState,
    directive: Directive,
    result_msg: str,
    *,
    scrub_replaced: bool = True,
) -> str:
    """标准 INV-3 纠正重跑：产出可交付新文本则替换、下发并剥旧答，否则保留原答。"""
    logger.warning(i18n_t("log.agent.fakedone_call_action_appending_ok"))
    prior = result_msg.strip()
    corrected = await _try_correction_pass_for(
        host,
        st,
        (directive,),
        suppress_intermediate_text=True,
        return_mode="return",
    )
    next_msg = corrected_or_original(corrected, original=result_msg, user_text=turn_user_text(st))
    if next_msg.strip() != prior:
        await _emit_unsent_reply_for(host, st, next_msg)
        host._scrub_fake_done_history({prior} if (scrub_replaced and prior) else set())
    else:
        host._scrub_fake_done_history(set())
    return next_msg


@dataclass(frozen=True, slots=True)
class _SettleRingBridge:
    """将 RunOnceHost 适配为 SettleRingHost，避免跨 Phase 继承桩耦合。"""

    host: RunOnceHost

    @property
    def persona_name(self) -> str | None:
        return self.host.persona_name

    @property
    def create_by(self) -> str:
        return self.host.create_by

    @property
    def sent_texts(self) -> set[str]:
        return self.host._run_sent_texts

    @property
    def dispute_count(self) -> int:
        return len(self.host._run_disputes)

    def latest_dispute_preview(self) -> str:
        return self.host._run_disputes[-1][:120] if self.host._run_disputes else ""

    @property
    def attempt_tool_calls(self) -> Sequence[str]:
        return self.host._last_attempt_tool_calls

    def model_declares_video(self) -> bool:
        return self.host._model_declares_video()

    async def try_correction_pass(
        self,
        st: RunOnceState,
        directives: tuple[Directive, ...],
        *,
        suppress_intermediate_text: bool = True,
        return_mode: ReturnMode = "return",
    ) -> object:
        return await _try_correction_pass_for(
            self.host,
            st,
            directives,
            suppress_intermediate_text=suppress_intermediate_text,
            return_mode=return_mode,
        )

    async def emit_unsent_reply(self, st: RunOnceState, text: str) -> None:
        await _emit_unsent_reply_for(self.host, st, text)

    async def run_inv3_correction(
        self,
        st: RunOnceState,
        directive: Directive,
        result_msg: str,
        *,
        scrub_replaced: bool = True,
    ) -> str:
        return await _run_inv3_correction_for(
            self.host,
            st,
            directive,
            result_msg,
            scrub_replaced=scrub_replaced,
        )

    def scrub_fake_done_history(self, fabricated: set[str]) -> None:
        self.host._scrub_fake_done_history(fabricated)

    def note_output_block(self, st: RunOnceState, reason: str, body: str) -> None:
        record_output_block(st, self.host._session_logger, reason, body)


def _record_prefix_break_probe_for(host: RunOnceHost, st: RunOnceState, new_msgs: List[ModelMessage]) -> None:
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
    system_hash = hash_system_prompt(host.system_prompt or "")
    hist_hashes = hash_history_messages(host.history)
    prev = host._prefix_snapshot
    reason, break_pos, approx_tokens_before = classify_prefix_break_with_pos(
        prev,
        history_hashes=hist_hashes,
        tools_hash=tools_hash,
        system_hash=system_hash,
        prev_payloads=prev.payloads if prev is not None else (),
        curr_payloads=history_payloads(host.history),
    )
    diff: dict[str, list[str]] | None = None
    if prev is not None and reason == "tools":
        diff = _tools_diff(prev.tool_names, st.tool_names)
        removed = diff["removed"] if "removed" in diff else []
        if removed:
            logger.warning(i18n_t("log.agent.prefix_tools_removed", names=removed[:12]))
    record_prefix_break(reason)
    host._session_logger.log_prefix_break(
        reason,
        tools_hash=tools_hash,
        system_hash=system_hash,
        tools_diff=diff,
        break_pos=break_pos,
        approx_tokens_before=approx_tokens_before,
    )
    combined = list(host.history) + list(new_msgs)
    host._prefix_snapshot = PrefixSnapshot(
        history_hashes=hash_history_messages(combined),
        tools_hash=tools_hash,
        system_hash=system_hash,
        payloads=history_payloads(combined),
        tool_names=list(st.tool_names),
    )


async def _settle_history_and_output_gate(host: RunOnceHost, st: RunOnceState, result: Any) -> None:
    """写入前缀快照、精简历史并收尾输出闸门。"""
    new_msgs = result.new_messages()
    _record_prefix_break_probe_for(host, st, new_msgs)
    _relean_user_turn(
        new_msgs,
        st.lean_user_message,
        strip_hint_texts=(
            _THRASH_FUSE_NUDGE,
            *output_gate.GATE_NUDGE_MARKERS,
            *_correction_nudge_markers(),
        ),
    )
    new_msgs = [m for m in new_msgs if not (isinstance(m, ModelRequest) and len(m.parts) == 0)]
    new_msgs = _dedupe_delivery_cards(new_msgs)
    new_msgs = _drop_unsent_text_from_tail(new_msgs, st.unsent_texts)

    # 前缀缓存红线（§1.7）：主会话禁止事后改写已入缓存的 ToolReturnPart
    host.history.extend(new_msgs)
    ctx_dyn = st.context
    if ctx_dyn is not None:
        host._last_run_find_tools_names = sorted(ctx_dyn.dynamic_tool_names)

    st.ab_abort = await host._resolve_output_gate_after_run(
        _require_context(st),
        st.bot,
        st.ev,
        return_mode=st.return_mode,
        ooc_blocked=st.ooc_blocked,
        ab_abort=st.ab_abort or output_gate.is_fused(_require_context(st).extra),
    )


def _update_post_run_tool_signals(host: RunOnceHost, st: RunOnceState) -> None:
    """回填能力族黏性与连续无工具轮次信号。"""
    if st.tool_call_list:
        for tname in set(st.tool_call_list):
            tb = find_tool_base(tname)
            dom = tb.capability_domain if tb else None
            if dom:
                host._recent_tool_families[dom] = _STICKY_FAMILY_TURNS

    if host.create_by in ["Chat", "Agent"] and st.intent not in _PROGRESSIVE_TOOLS_SKIP_INTENTS:
        if st.tool_call_list:
            host._consecutive_no_tool_rounds = 0
            host._unmet_tool_promise = False
        else:
            host._consecutive_no_tool_rounds += 1
            thinking_blob = "\n".join(st.thinking_segments)
            mentioned = bool(thinking_blob and st.tool_names and any(tn in thinking_blob for tn in st.tool_names))
            host._unmet_tool_promise = mentioned
            if mentioned:
                logger.debug(i18n_t("log.agent.intent_action_mismatch_force"))


async def _record_settle_usage_and_latency(
    host: RunOnceHost,
    st: RunOnceState,
    result: Any,
    statistics_manager: Any,
) -> None:
    """结算单次 run 的延迟、Token 统计、预算记账与会话日志。"""
    latency = time.time() - st.start_time
    statistics_manager.record_latency(latency=latency)

    try:
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

        is_nested = bool(host.is_subagent) or (bool(st.user_turn_id) and not st.owns_user_turn)
        if input_tokens > 0 or output_tokens > 0:
            statistics_manager.record_token_usage(
                model_name=st.model_name,
                chat_type=st.stats_chat_type if st.stats_chat_type else host.create_by,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                request_count=max(1, int(usage_obj.requests)),
            )
        statistics_manager.record_agent_run(
            owns_user_turn=bool(st.owns_user_turn),
            under_user_turn=bool(st.user_turn_id),
            is_nested=is_nested,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        if st.budget_scope is not None:
            try:
                from gsuid_core.ai_core.budget import budget_manager

                await budget_manager.record_usage_scope(
                    st.budget_scope[0],
                    st.budget_scope[1],
                    st.budget_scope[2],
                    host.session_id,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                )
            except Exception as be:
                logger.warning(i18n_t("log.agent.budget_fail", _be=be))
        try:
            host._session_logger.log_token_usage(
                input_tokens,
                output_tokens,
                st.model_name,
                cache_read_tokens,
                cache_write_tokens,
            )
        except Exception as le:
            logger.debug(i18n_t("log.agent.write_token_usage_log", _le=le))
    except AttributeError as e:
        logger.info(i18n_t("log.agent.access_result_usage", e=e))
    except TypeError as e:
        logger.info(i18n_t("log.agent.result_usage_call_style", e=e))
    except Exception as e:
        logger.warning(i18n_t("log.agent.record_statistics", e=e))


def _record_settle_journal_for(host: RunOnceHost, st: RunOnceState, final_msg: str) -> None:
    """在策略环、OOC 防火墙与协议泄漏剥离全部通过后，把合规实质行动写入群聊或私聊手账。"""
    if st.ab_abort or st.ev is None:
        return
    is_direct = st.ev.user_type == "direct" or not st.ev.group_id
    skip_role = host.is_subagent or host.create_by in (
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
    from gsuid_core.ai_core.inbox.turn import current_turn
    from gsuid_core.ai_core.journal.manager import (
        record_journal_entry,
        resolve_persona_for_group_or_session,
    )
    from gsuid_core.ai_core.journal.storage import JournalEntry
    from gsuid_core.ai_core.journal.sanitizer import (
        clean_journal_tag,
        is_rejected_journal_body,
        sanitize_journal_summary,
    )

    gid = "" if is_direct else str(st.ev.group_id).strip()
    journal_turn = current_turn()
    journal_active = journal_turn.active_entry() if journal_turn is not None else None
    req_uid = (
        journal_active.item.user_id.strip()
        if journal_active is not None and journal_active.item.user_id
        else (str(st.ev.user_id).strip() if st.ev.user_id else "")
    )
    # 私聊无明确 user_id 时 fail-closed 丢弃，防止落入公共 self.md
    if is_direct and not req_uid:
        return

    meta_tools = frozenset(
        {"find_tools", "call_tool", "search_cognition", "read_handle", "read_life_journal", "dispute_directive"}
    )
    substantive_tools = [tn for tn in (st.tool_call_list or []) if tn not in meta_tools]
    ordered_bubbles: list[str] = [t_s.strip() for t_s in host._run_sent_order if t_s.strip() and "<SILENCE>" not in t_s]
    for extra_txt in sorted(host._run_sent_texts):
        clean_extra = extra_txt.strip()
        if clean_extra and "<SILENCE>" not in clean_extra and clean_extra not in ordered_bubbles:
            ordered_bubbles.append(clean_extra)

    img_delivery_text = "已完成私聊图片交付" if is_direct else "已完成群内图片交付"
    if st.return_mode in ("by_bot",) and st.bot is not None:
        if ordered_bubbles:
            raw_out = "；".join(ordered_bubbles)
        elif st.image_sent_this_run:
            raw_out = img_delivery_text
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
            raw_out = img_delivery_text
        else:
            return

    if not raw_out or is_silence_marker(raw_out) or is_rejected_journal_body(raw_out):
        return

    nicks_for_j: dict[str, str] = {}
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
        raw_t = tb0.capability_domain if tb0 and tb0.capability_domain else (tb0.name if tb0 else substantive_tools[0])
        j_tag = clean_journal_tag(raw_t or "日常行动")
    elif st.image_sent_this_run:
        j_tag = "图文交付"
    else:
        j_tag = "私聊答疑" if is_direct else "群聊答疑"

    target_persona = host.persona_name.strip() if host.persona_name else ""
    if not target_persona:
        resolved_persona = resolve_persona_for_group_or_session(
            session_id=st.ev.session_id,
            group_id=gid or None,
        )
        target_persona = resolved_persona or ""
    if is_direct and not target_persona:
        return

    now = datetime.now()
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
            target_personas=[target_persona] if target_persona else None,
        )
    )


def _attach_unfinished_tool_return_for(host: RunOnceHost, st: RunOnceState, result_msg: str) -> str:
    """能力代理没写成结论时，把最后一次工具返回交回调用方。"""
    from gsuid_core.ai_core.agent_run.support import _UNFINISHED_CAPABILITY_BANNER

    text = str(result_msg).strip()
    if host.create_by != "CapabilityAgent" or st.return_mode != "return":
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


def _log_run_result_for(host: RunOnceHost, st: RunOnceState, result_msg: str) -> None:
    """沉默或空输出时把原因写进 result。有正文则不带 silence_reason。"""
    from gsuid_core.ai_core.agent_run.support import result_silence_reason

    reason = result_silence_reason(
        str(result_msg),
        existing=st.silence_reason,
        blocked=st.last_block_reason,
    )
    host._session_logger.log_result(result_msg, st.tool_call_list, silence_reason=reason)


def _remember_outbound_on_silence_for(host: RunOnceHost, st: RunOnceState, result_msg: str) -> None:
    """静默不进 A 轨；把本轮出站句柄补上，追问才能 read_handle。"""
    if host.is_subagent or host.create_by not in ("Chat", "Agent"):
        return
    if not result_msg or not is_silence_marker(result_msg.strip()):
        return
    ev = st.ev
    if ev is None or not ev.group_id:
        return
    from gsuid_core.ai_core.history_format import noted_thread_handles, remember_silence_handles

    remember_silence_handles(ev, noted_thread_handles(st.context))


def _pure_meme_reply_expected(st: RunOnceState) -> bool:
    """被寻址/私聊轮才要求纯表情未命中时改口短台词；群聊旁观轮允许静默。"""
    if st.blocks_user_speech or st.ev is None:
        return False
    if not st.ev.group_id:
        return True
    if st.tg is not None:
        return bool(
            st.tg.ellipsis_followup or (st.tg.call_to_self and not st.tg.quoted_tome and not st.tg.address_gated)
        )
    return bool(st.ev.is_tome)


async def _resolve_pure_meme_settle_for(
    ring_host: SettleRingHost,
    st: RunOnceState,
    result_msg: str,
    *,
    is_subagent: bool = False,
    sent_order: list[str] | None = None,
) -> str:
    """统一收口延迟到 settle 的纯表情回复：命中则预选发出，未命中则按寻址规则重说或静默。"""
    if st.ab_abort or is_subagent or ring_host.create_by not in _MAIN_PERSONA_CREATE_BY:
        return result_msg
    if st.bot is None or st.ev is None or st.return_mode not in ("always", "by_bot"):
        return result_msg

    prior = result_msg.strip()
    if not prior or prior in ring_host.sent_texts:
        return result_msg
    pure_query = extract_pure_meme_query(prior)
    if pure_query is None:
        return result_msg

    from gsuid_core.ai_core.meme.selector import pick
    from gsuid_core.ai_core.persona.config import persona_allows_meme_reply
    from gsuid_core.ai_core.buildin_tools.meme_tools import _get_persona_for_event

    persona = _get_persona_for_event(st.ev)
    if not persona_allows_meme_reply(persona):
        pre_record, reason = None, "persona_disabled"
    else:
        try:
            pre_record, reason = await pick(
                query=pure_query,
                persona=persona,
                session_id=st.ev.session_id,
                ignore_cooldown=True,
                commit=False,
                is_pure_meme=True,
            )
        except Exception as e:
            # 检索链路（Qdrant/SQL/嵌入）抖动不能拖垮整轮回复，按未命中走改口或静默
            logger.warning(i18n_t("log.ai.meme_pick_fail", e=e))
            pre_record, reason = None, "pick_error"

    if pre_record is not None:
        from gsuid_core.ai_core.meme.library import get_memes_base_path

        if not (get_memes_base_path() / pre_record.file_path).is_file():
            pre_record = None
            reason = "file_missing"

    if pre_record is not None:
        raw_at = st.run_extra["at_user_id"] if "at_user_id" in st.run_extra else None
        at_uid = str(raw_at) if isinstance(raw_at, str) and raw_at else None
        await send_chat_result(
            st.bot,
            prior,
            ev=st.ev,
            at_user_id=at_uid,
            extra_metadata=turn_reply_metadata(st.ev),
            preselected_meme=pre_record,
        )
        absorb_run_image_delivered(st)
        ring_host.sent_texts.add(prior)
        if sent_order is not None and prior not in sent_order:
            sent_order.append(prior)
        st.main_channel_sends += 1
        return result_msg

    scrub_targets = {prior}
    if (
        st.main_channel_sends == 0
        and not st.image_sent_this_run
        and not st.fake_done_retry
        and _pure_meme_reply_expected(st)
    ):
        corrected = await ring_host.try_correction_pass(
            st,
            (pure_meme_no_match_directive(pure_query),),
            suppress_intermediate_text=True,
            return_mode="return",
        )
        next_msg = corrected_or_original(corrected, original=result_msg, user_text=turn_user_text(st))
        next_stripped = next_msg.strip()
        if next_stripped != prior and not is_silence_marker(next_msg) and extract_pure_meme_query(next_msg) is None:
            await ring_host.emit_unsent_reply(st, next_msg)
            ring_host.scrub_fake_done_history({prior})
            return next_msg
        if next_stripped:
            scrub_targets.add(next_stripped)

    ring_host.scrub_fake_done_history(scrub_targets)
    block_reason = f"pure_meme_{reason}"
    if st.main_channel_sends == 0 and not st.image_sent_this_run:
        st.silence_reason = block_reason
    ring_host.note_output_block(st, block_reason, prior)
    return "<SILENCE>"


async def _finalize_settle_text(
    host: RunOnceHost,
    st: RunOnceState,
    raw_output: object,
    *,
    ring_host: SettleRingHost,
) -> str:
    """执行策略环、INV-4 暂扣补发、OOC/协议泄漏清洗并落手账与日志。"""
    result_msg = str(raw_output).strip()
    match_msg = strip_protocol_tags(result_msg).strip()
    if st.tool_call_list:
        logger.debug(i18n_t("log.agent.current_tool_call_event", p0=", ".join(st.tool_call_list)))

    host._session_logger.log_run_end()

    from gsuid_core.ai_core.agent_node.registry import persona_allows_capability_agent

    allow_render = (
        not ("delivery_failed" in st.run_extra and st.run_extra["delivery_failed"] is True)
    ) and persona_allows_capability_agent(host.persona_name, "render_agent")

    async def _resend_fab_blocked() -> None:
        for bt in st.fab_blocked:
            if bt in host._run_sent_texts:
                continue
            try:
                if st.bot is None:
                    logger.warning(i18n_t("log.agent.fakedone_bot_object_unavailable"))
                    continue
                await send_chat_result(st.bot, bt, ev=st.ev, extra_metadata=turn_reply_metadata(st.ev))
                absorb_run_image_delivered(st)
                host._run_sent_texts.add(bt)
                if bt not in host._run_sent_order:
                    host._run_sent_order.append(bt)
                st.main_channel_sends += 1
            except Exception as se:
                logger.debug(i18n_t("log.agent.fakedone_se", _se=se))

    ring_out = await run_settle_policy_ring(
        ring_host,
        SettleRingInput(
            st=st,
            result_msg=result_msg,
            match_msg=match_msg,
            allow_render=allow_render,
            resend_fab_blocked=_resend_fab_blocked,
        ),
    )
    result_msg = ring_out.result_msg

    if _should_deliver_withheld(
        st,
        skip_report_exit=ring_out.skip_report_exit,
        replacement_visible=ring_out.replacement_visible,
    ):
        await _deliver_withheld(st, host._run_sent_texts, host._run_sent_order)

    result_msg = await _resolve_pure_meme_settle_for(
        ring_host,
        st,
        result_msg,
        is_subagent=host.is_subagent,
        sent_order=host._run_sent_order,
    )

    if st.return_mode in ["by_bot"] and st.bot and st.ev:
        if st.main_channel_sends > 0 or st.image_sent_this_run:
            from gsuid_core.ai_core.planning.kanban_executor import mark_inline_claimed_roots_delivered

            mark_inline_claimed_roots_delivered(st.run_extra)
        _record_settle_journal_for(host, st, result_msg)
        _log_run_result_for(host, st, result_msg)
        return ""

    if result_msg and is_ooc_firewall_enabled() and host.create_by not in ("EvalJudge", "TEST"):
        skip_roleplay_scrub = host.is_subagent or host.create_by in ("CapabilityAgent", "AutoPlanner")
        if skip_roleplay_scrub:
            if is_tech_dump(match_msg):
                result_msg = "⚠️ 子任务返回技术错误堆栈，已屏蔽。请主人格换路或重试（勿向用户念本句）。"
        else:
            hit = inspect_outbound_ooc(
                match_msg,
                user_text=st.ev.raw_text if st.ev is not None and st.ev.raw_text else "",
            )
            if hit is not None:
                logger.warning(i18n_t("log.agent.firewall_run_return_value_hit", p0=hit.category))
                ooc_original = result_msg
                recovered = await host._ooc_recover_persona_voice(hit, result_msg, st.ev) or "<SILENCE>"
                had_body = bool(ooc_original.strip()) and not is_silence_marker(ooc_original.strip())
                if had_body and (not recovered.strip() or is_silence_marker(recovered.strip())):
                    st.silence_reason = "ooc_recover"
                    record_output_block(st, host._session_logger, "ooc_recover", ooc_original)
                result_msg = recovered

    if isinstance(result_msg, str) and not (host.is_subagent or host.create_by in ("CapabilityAgent", "AutoPlanner")):
        leak_original = result_msg
        leaked = strip_framework_user_leaks(result_msg)
        had_body = bool(leak_original.strip()) and not is_silence_marker(leak_original.strip())
        result_msg = leaked
        if not result_msg.strip():
            if had_body:
                st.silence_reason = "framework_leak_stripped"
                record_output_block(st, host._session_logger, "framework_leak_stripped", leak_original)
            result_msg = "<SILENCE>"

    if isinstance(result_msg, str):
        if (
            st.main_channel_sends > 0
            or st.image_sent_this_run
            or (
                st.return_mode in ("return", "always")
                and bool(result_msg.strip())
                and not is_silence_marker(result_msg.strip())
            )
        ):
            from gsuid_core.ai_core.planning.kanban_executor import mark_inline_claimed_roots_delivered

            mark_inline_claimed_roots_delivered(st.run_extra)
        _record_settle_journal_for(host, st, result_msg)
    result_msg = _attach_unfinished_tool_return_for(host, st, result_msg)
    _log_run_result_for(host, st, result_msg)
    if isinstance(result_msg, str):
        _remember_outbound_on_silence_for(host, st, result_msg)
    return result_msg


async def _note_truncated_usage_for(
    host: RunOnceHost,
    st: RunOnceState,
    statistics_manager: Any,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    request_count: int = 1,
    count_run: bool,
    close_chat: bool,
) -> None:
    """把一段用量写入统计和会话日志。close_chat 只在整轮合计时用一次。"""
    model = st.model_name or "unknown"
    chat_type = st.stats_chat_type if st.stats_chat_type else host.create_by
    if count_run:
        nested = bool(host.is_subagent) or (bool(st.user_turn_id) and not st.owns_user_turn)
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
            request_count=max(1, request_count),
        )
        if st.budget_scope is not None:
            try:
                from gsuid_core.ai_core.budget import budget_manager

                await budget_manager.record_usage_scope(
                    st.budget_scope[0],
                    st.budget_scope[1],
                    st.budget_scope[2],
                    host.session_id,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                )
            except Exception as exc:
                logger.warning(i18n_t("log.agent.budget_fail", _be=exc))
    if close_chat:
        host._session_logger.log_node_transition("End")
    has_usage = input_tokens > 0 or output_tokens > 0 or cache_read_tokens > 0 or cache_write_tokens > 0
    if has_usage or (model and model != "unknown"):
        host._session_logger.log_token_usage(
            input_tokens,
            output_tokens,
            model,
            cache_read_tokens,
            cache_write_tokens,
        )


async def _capability_salvage_material_for(host: RunOnceHost, st: RunOnceState) -> str:
    """优先用本会话已落盘的检索原文（按新到旧）。没有再退回本轮摘录。"""
    persisted = await load_session_salvage_material(host.session_id or "")
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


def _build_toolless_fallback_agent(host: RunOnceHost) -> Agent[None, str]:
    """构建无工具收口 Agent，统一超轮数兜底与无材料致歉路径。"""
    return Agent(
        model=host.model,
        system_prompt=host.system_prompt or "你是一个智能助手。",
        model_settings=ModelSettings(max_tokens=int(host.max_tokens)),
        tools=[],
        toolsets=[],
        retries=0,
        output_type=str,
    )


async def _run_toolless_fallback_agent(host: RunOnceHost, prompt: str) -> object:
    return await _build_toolless_fallback_agent(host).run(
        prompt,
        message_history=list(host.history),
        usage_limits=UsageLimits(request_limit=1),
    )


async def _emit_fallback_text_or_silence(
    host: RunOnceHost,
    st: RunOnceState,
    text: str,
    *,
    silence_reason: str,
) -> str:
    if not text.strip() or is_silence_marker(text.strip()):
        _remember_outbound_on_silence_for(host, st, "<SILENCE>")
        host._session_logger.log_result(
            "<SILENCE>",
            st.tool_call_list,
            silence_reason=st.silence_reason or silence_reason,
        )
        return "<SILENCE>"
    host._session_logger.log_text_output(text)
    host._session_logger.log_result(text, st.tool_call_list)
    if st.bot:
        await send_chat_result(st.bot, text, ev=st.ev, extra_metadata=turn_reply_metadata(st.ev))
        absorb_run_image_delivered(st)
    return ""


async def _salvage_capability_delivery_for(host: RunOnceHost, st: RunOnceState) -> str | None:
    """有材料时收成交付交回调用方。返回 None 表示仍走可识别失败。"""
    if st.return_mode != "return":
        return None
    if st.delegated_render and not st.image_sent_this_run:
        return None
    if host.create_by not in ("CapabilityAgent", "AutoPlanner"):
        return None
    material = await _capability_salvage_material_for(host, st)
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
    from pydantic_ai.run import AgentRunResult

    result = await _run_toolless_fallback_agent(host, prompt)
    if isinstance(result, AgentRunResult):
        usage = result.usage
        st.usage_requests += max(1, int(usage.requests))
        st.usage_input_tokens += usage.input_tokens
        st.usage_output_tokens += usage.output_tokens
        st.usage_cache_read_tokens += usage.cache_read_tokens
        st.usage_cache_write_tokens += usage.cache_write_tokens
    raw_out = result.output if isinstance(result, AgentRunResult) else ""
    text = str(raw_out).strip()
    if not text or is_silence_marker(text):
        return None
    if not text.startswith("⚠"):
        text = f"{_USAGE_LIMIT_SALVAGE_BANNER}\n\n{text}"
    from gsuid_core.ai_core.buildin_tools.subagent import looks_like_incomplete_subagent_delivery

    if looks_like_incomplete_subagent_delivery(text):
        return None
    logger.info(i18n_t("log.agent.usage_limit_salvage"))
    return text


def _no_material_reply_expected(st: RunOnceState) -> bool:
    """本轮无材料时，用户明确在等回答吗（私聊 / @点名 / 省略续聊）。"""
    if st.ev is not None and not st.ev.group_id:
        return True
    tg = st.tg
    if tg is None:
        return False
    return bool(tg.call_to_self or tg.ellipsis_followup)


async def _deliver_no_material_reply_for(host: RunOnceHost, st: RunOnceState, user_question: str) -> str:
    """无材料但对方在等：给一句角色口吻的「没答上来」，不解释、不编造。"""
    from pydantic_ai.run import AgentRunResult

    msg = (
        f"【用户的问题】\n{user_question}\n\n"
        "你没查到任何材料，也还没想出结论。用人设口吻承认这次没答上来，"
        "一句话、20 字以内。禁止编造内容，禁止解释为什么没查到，"
        "禁止提到检索、工具、数据库一类的东西。"
    )
    result = await _run_toolless_fallback_agent(host, msg)
    text = str(result.output) if isinstance(result, AgentRunResult) else ""
    return await _emit_fallback_text_or_silence(host, st, text, silence_reason="no_material_reply_silence")


class SettlePhase:
    async def _run_once_settle_result(
        self: RunOnceHost,
        st: RunOnceState,
        agent_run: Any,
        statistics_manager: Any,
    ) -> Any:
        """iter 成功路径：history / 闸门 / token / 假完成 / OOC / return。"""
        absorb_run_image_delivered(st)
        result = agent_run.result
        if not result:
            return NO_RESULT_TEXT

        logger.info(i18n_t("log.agent.iter_ok"))
        await _settle_history_and_output_gate(self, st, result)
        _update_post_run_tool_signals(self, st)
        await _record_settle_usage_and_latency(self, st, result, statistics_manager)

        if st.output_type is not None:
            self._session_logger.log_run_end()
            self._session_logger.log_result(result.output, st.tool_call_list)
            return result.output

        return await _finalize_settle_text(self, st, result.output, ring_host=_SettleRingBridge(self))

    async def _run_once_usage_limit_fallback(
        self: RunOnceHost,
        st: RunOnceState,
        statistics_manager: Any,
    ) -> Any:
        """UsageLimitExceeded 专属兜底总结。"""
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
            salvaged = await _salvage_capability_delivery_for(self, st)

        await _note_truncated_usage_for(
            self,
            st,
            statistics_manager,
            input_tokens=st.usage_input_tokens,
            output_tokens=st.usage_output_tokens,
            cache_read_tokens=st.usage_cache_read_tokens,
            cache_write_tokens=st.usage_cache_write_tokens,
            request_count=max(1, st.usage_requests),
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
            _remember_outbound_on_silence_for(self, st, "<SILENCE>")
            st.silence_reason = st.silence_reason or "delegated_render_unfinished"
            _log_run_result_for(self, st, "<SILENCE>")
            return "<SILENCE>"

        logger.debug(i18n_t("log.agent.chain_too_long_forced_summary", p0=_require_limits(st).request_limit))
        user_question = st.last_user_question or "用户之前提出的问题"
        tool_outputs = "\n".join(st.run_tool_outputs)
        thinking = collect_run_thinking(st.thinking_segments)
        material = ""
        if tool_outputs:
            material += f"【本轮已查到的内容】\n{tool_outputs}"
        if thinking:
            material += f"\n\n【本轮的推理线索】\n{thinking}"

        if not material:
            if _no_material_reply_expected(st):
                return await _deliver_no_material_reply_for(self, st, user_question)
            logger.debug(i18n_t("log.agent.chain_too_long_no_context_silence"))
            _remember_outbound_on_silence_for(self, st, "<SILENCE>")
            st.silence_reason = st.silence_reason or "usage_limit_no_material"
            _log_run_result_for(self, st, "<SILENCE>")
            return "<SILENCE>"

        from pydantic_ai.run import AgentRunResult

        final_message = (
            f"【用户的问题】\n{user_question}\n\n{material}\n\n"
            "请只根据以上材料回答用户的问题，人设风格不变。材料里没有的就说没有，"
            "禁止补充任何未在材料中出现的信息。禁止调用任何工具，只输出自然语言文本。"
        )
        fallback_result = await _run_toolless_fallback_agent(self, final_message)
        fallback_text = str(fallback_result.output) if isinstance(fallback_result, AgentRunResult) else ""
        return await _emit_fallback_text_or_silence(
            self,
            st,
            fallback_text,
            silence_reason="usage_limit_fallback_silence",
        )

    def _run_once_cleanup(self: RunOnceHost, st: RunOnceState) -> None:
        """finally：还原 budget scope / 墙钟 / 单轮节流。"""
        self._last_attempt_delegated_render = st.delegated_render
        self._last_attempt_image_sent = st.image_sent_this_run
        self._last_attempt_pending_async = st.pending_async_delivery
        self._last_attempt_effectual_mutate = st.effectual_mutate
        self._last_attempt_has_status_tool = st.has_status_tool_call
        self._last_attempt_tool_outcomes = (frozenset(st.succeeded_tools), frozenset(st.failed_tools))
        from gsuid_core.ai_core.const import THINKING_TEXT_MAX

        thinking_blob = "\n".join(s for s in st.thinking_segments if s)
        self._last_attempt_thinking = thinking_blob[-THINKING_TEXT_MAX:] if thinking_blob else ""
        if st.budget_scope_token is not None:
            _current_budget_scope.reset(st.budget_scope_token)
        if st.wall_token is not None:
            wall_clock.uninstall_clock(st.wall_token)
            st.wall_token = None
        if st.user_turn_token is not None:
            reset_user_turn_id(st.user_turn_token)
            st.user_turn_token = None
        try:
            from gsuid_core.ai_core.buildin_tools.scheduler import clear_turn_throttle
            from gsuid_core.ai_core.buildin_tools.message_sender import clear_turn_send_throttle

            sess = st.ev.session_id if st.ev is not None else None
            if sess:
                clear_turn_throttle(str(sess), st.turn_id)
                clear_turn_send_throttle(str(sess), st.turn_id)
        except Exception as _e:
            logger.debug(i18n_t("log.agent.clear_counter", _e=_e))
