"""AI 聊天入口：**薄编排器**。

本模块只做编排——并发闸 / TTL / 预算 / Session / 长度防护 / 开口门 / 结算调用 /
出站分发，以及在固定锚点 ``fire_hooks``。产品能力（记忆、关系温度、情绪、意图分类、
软触发门、脚手架注入、工具装配）全部是**套件**，挂在 hook 总线上。

``run_interactive_turn`` 是生产 ``handle_inbox_turn`` 与评测
``/api/chat_with_history`` 共用的**唯一一轮编排**。评测只允许在进这个函数之前
做适配（建 Event / 灌 history / 夹具 View），不许再自己分类、检索或结算。

三条纪律：
1. 内核函数体里不出现 ``dual_route_retrieve`` / ``update_mood`` / ``classifier_service``
   这类产品 import——关某个能力靠**槽位不注册**，不靠在内核里写 ``if enable_x``。
   ``settle_turn`` / ``RelationshipView`` 是冻结写主，留在本模块是有意的。
2. 每轮动态内容只进 user 侧；``system_prompt`` 会话内绝不改串。
3. 结算是唯一写主：负信号不受「是否有效互动」限制，早退路径也要结算。
"""

import time
import asyncio
from typing import Literal, Optional
from dataclasses import dataclass

# 导入表情包模块以注册 on_core_shutdown 钩子和 @ai_tools
import gsuid_core.ai_core.meme.startup  # noqa: F401
import gsuid_core.ai_core.buildin_tools.meme_tools  # noqa: F401
from gsuid_core.bot import Bot, _Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.hooks import HookDecision, AgentHookPoint, AgentHookContext, fire_hooks
from gsuid_core.ai_core.utils import prepare_media_payload, has_model_visible_content
from gsuid_core.ai_core.startup import is_ai_core_ready, wait_ai_core_ready
from gsuid_core.message_history import MessageRecord, get_history_manager
from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
from gsuid_core.ai_core.ai_router import get_ai_session
from gsuid_core.ai_core.inbox.turn import (
    InboxTurn,
    bind_turn,
    reset_turn,
    single_turn_from_event,
)
from gsuid_core.ai_core.thread_view import render_delta, render_router_view
from gsuid_core.ai_core.relationship import RelationshipView, fetch_relationship, collect_priority_speakers
from gsuid_core.message_history.link import EventLink
from gsuid_core.ai_core.content_guard import GuardFlags, annotate_untrusted_message_ex
from gsuid_core.ai_core.turn_pipeline import (
    evaluate_budget,
    check_budget_gate,
    deliver_run_result,
    stamp_current_time,
    apply_summary_guard,
    classify_run_result,
    prev_turn_used_tools,
    recent_report_titles,
    apply_absolute_length_guard,
    build_private_history_block,
    prior_user_turns_for_intent,
    extract_pending_unread_images,
    prior_turns_from_agent_history,
)
from gsuid_core.ai_core.trigger_bridge import MockBot
from gsuid_core.ai_core.followup_window import (
    note_hard_trigger,
    record_soft_reply,
    record_gate_outcome,
    record_gate_silence,
)
from gsuid_core.ai_core.inbox.scheduler import TurnOutcome, set_turn_runner
from gsuid_core.ai_core.context_assembly import assemble_dynamic_context
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.angle_bracket_guard import strip_protocol_tags
from gsuid_core.ai_core.relationship.engine import settle_turn
from gsuid_core.ai_core.interaction_scaffold import (
    CheapGate,
    GroupOpenGate,
    build_turn_graph,
    decide_cheap_gate,
    has_recent_tool_call,
    recent_history_texts,
)

# AI并发控制配置
MAX_CONCURRENT_AI_CALLS = 10  # 全局最大并发AI调用数
_ai_semaphore = asyncio.Semaphore(MAX_CONCURRENT_AI_CALLS)  # AI并发信号量


def interactive_llm_in_use() -> bool:
    # Semaphore 没有公开的占用数。_value 是剩余许可，小于上限说明聊天还占着槽。
    return _ai_semaphore._value < MAX_CONCURRENT_AI_CALLS


@dataclass
class InteractiveTurnResult:
    """``run_interactive_turn`` 的一轮结果。评测入口据此组 HTTP，生产入口据此出站。"""

    result: str
    result_text: str
    is_silence: bool
    is_error: bool
    intent: str
    silenced_early: bool
    hook_ctx: AgentHookContext
    rel: Optional[RelationshipView]


@dataclass
class PassiveChatResult:
    """``run_passive_interactive_chat`` 的早退/完成结果（H01 之前到 turn）。"""

    status: Literal["ok", "silence", "disabled", "not_ready", "budget"]
    turn: Optional[InteractiveTurnResult] = None


async def _wait_core_ready() -> bool:
    """AI 核心未就绪时等待迁移完成，避免迁移期间处理聊天触发旧向量查询。"""
    try:
        if is_ai_core_ready():
            return True
        logger.info(t("log.ai.gscore_init_done_migrate_msg_ai"))
        if not await wait_ai_core_ready(timeout=300.0):
            logger.warning(t("log.ai.gscore_core_initialization_wait"))
            return False
    except Exception as e:
        logger.warning(t("log.ai.gscore_check_core_init", e=e))
    return True


async def run_passive_interactive_chat(
    bot: Bot,
    event: Event,
    *,
    enqueue_ts: Optional[float] = None,
    soft_triggered: bool = False,
    budget_mode: Literal["gate", "decision", "skip"] = "gate",
    wall_clock_budget: Optional[float] = None,
    return_mode: Literal["always", "return", "by_bot"] = "by_bot",
    deliver: bool = True,
    settle: bool = True,
    outbound_stream: bool = False,
    stats_chat_type: Optional[str] = None,
    inbox_turn: Optional[InboxTurn] = None,
) -> PassiveChatResult:
    """适配器与 HTTP Agent 共用的被动聊天入口（不含 ``_ai_semaphore``）。

    顺序：enable / ready → 预算 → H01 → 长度 → 空内容门 → session → turn。
    HTTP 开流前已查预算时传 ``budget_mode="skip"``；墙钟仅 ``HTTP_AGENT:`` session。
    ``outbound_stream`` / ``stats_chat_type`` 由入口传入，loop 与用量记账读取。
    """
    if not ai_config.get_config("enable").data:
        logger.debug(t("log.ai.gscore_service_enabled_skipping"))
        return PassiveChatResult("disabled")
    if not await _wait_core_ready():
        return PassiveChatResult("not_ready")

    query = event.raw_text
    if budget_mode == "gate":
        if not await check_budget_gate(bot, event):
            return PassiveChatResult("budget")
    elif budget_mode == "decision":
        decision = await evaluate_budget(event)
        if decision is not None and not decision.allowed:
            return PassiveChatResult("budget")

    hook_ctx = AgentHookContext(
        point=AgentHookPoint.BEFORE_AI_CHAT,
        ev=event,
        bot=bot if isinstance(bot, Bot) else None,
        session_id=event.session_id,
        create_by="Chat",
        query=query,
        soft_triggered=soft_triggered,
    )
    if await fire_hooks(AgentHookPoint.BEFORE_AI_CHAT, hook_ctx) is not HookDecision.CONTINUE:
        return PassiveChatResult("silence")

    query = apply_absolute_length_guard(event, query)
    hook_ctx.query = query

    _is_at_me = bool(event.is_tome) or event.user_type == "direct"
    if not query.strip() and not has_model_visible_content(event) and not _is_at_me:
        logger.info(t("log.ai.gscore_empty_content_visible"))
        return PassiveChatResult("silence")

    if not event.group_id and event.user_id:
        from gsuid_core.ai_core.persona.settings import persona_name_from_event
        from gsuid_core.ai_core.proactive.dm_lease import renew_dm_lease, maybe_switch_dm_lease

        # 私聊开头显式点名其他启用人格 → 租约平滑切换（在 renew 与建 session 之前）
        maybe_switch_dm_lease(
            event.user_id, event.bot_id or "", event.raw_text or "", persona_name_from_event(event) or ""
        )
        renew_dm_lease(user_id=event.user_id, bot_id=event.bot_id or "")

    session = await get_ai_session(event)

    from gsuid_core.ai_core.persona.speaking_ctx import set_speaking_persona, reset_speaking_persona

    _speaking_token = set_speaking_persona(session.persona_name or "")
    try:
        # 用户回了私聊 → 清零该人格的未回连击（互惠退避解除）
        if not event.group_id and event.user_id and session.persona_name:
            from gsuid_core.ai_core.database.models import UserFavorability

            await UserFavorability.reset_dm_unanswered(event.user_id, bot.bot_id, session.persona_name)

        if wall_clock_budget is not None and event.session_id.startswith("HTTP_AGENT:"):
            session.wall_clock_budget = wall_clock_budget
        hook_ctx.persona_name = session.persona_name
        turn = await run_interactive_turn(
            bot=bot,
            event=event,
            session=session,
            query=query,
            hook_ctx=hook_ctx,
            soft_triggered=soft_triggered,
            enqueue_ts=enqueue_ts,
            return_mode=return_mode,
            deliver=deliver,
            settle=settle,
            outbound_stream=outbound_stream,
            stats_chat_type=stats_chat_type,
            inbox_turn=inbox_turn,
        )
        if turn.is_silence:
            return PassiveChatResult("silence", turn=turn)
        return PassiveChatResult("ok", turn=turn)
    finally:
        reset_speaking_persona(_speaking_token)


async def handle_inbox_turn(turn: InboxTurn, ws: _Bot) -> TurnOutcome:
    """收件箱轮次入口（异步队列执行）。**不抛**：边界内一切异常归一成
    ``TurnOutcome(error=True)``，调度器据此重试 / 过期。

    锚点顺序：预算 → H01 → 长度防护 → Session → TurnGraph/开口门 → H02 → H03 分类
    → 开口门重判 → H04 软门 → payload → H05 检索 → H06/H07 合成 → 开口门终判
    → ``session.run`` → 出站 → 结算 + H08。
    """
    primary = turn.primary
    bot = Bot(ws, primary.event)
    has_hard = any(e.item.strength == "hard" for e in turn.entries)
    async with _ai_semaphore:
        token = bind_turn(turn)
        from gsuid_core.ai_core.inbox.receipt import receipt_tool_policy, settle_receipt_entries
        from gsuid_core.ai_core.command_aside.policy import bind_tool_policy, restore_tool_policy

        # 回执批一律绑工具策略：allow_tools=False 的回执整轮禁工具；
        # 混有 hard 项时只挂附加工具不封锁（hard 需要工具）。
        pol_tokens = None
        if any(e.receipt is not None for e in turn.entries):
            _pol_allow, _pol_extra = receipt_tool_policy(turn)
            pol_tokens = bind_tool_policy(_pol_allow or has_hard, _pol_extra)
        try:
            result = await run_passive_interactive_chat(
                bot,
                primary.event,
                soft_triggered=not has_hard,
                inbox_turn=turn,
            )
            inner = result.turn
            if inner is not None and inner.result_text:
                settle_receipt_entries(turn, inner.result_text, turn.persona_name)
            return TurnOutcome(
                reached_model=turn.reached_model,
                error=bool(inner.is_error) if inner is not None else False,
                answered=frozenset(turn.answered),
            )
        except Exception as e:
            logger.exception(t("log.inbox.turn_failed", session_id=turn.session_id, error=str(e)))
            return TurnOutcome(
                reached_model=turn.reached_model,
                error=True,
                answered=frozenset(turn.answered),
            )
        finally:
            reset_turn(token)
            if pol_tokens is not None:
                restore_tool_policy(pol_tokens)


async def run_interactive_turn(
    *,
    bot: Bot,
    event: Event,
    session: GsCoreAIAgent,
    query: str,
    hook_ctx: AgentHookContext,
    soft_triggered: bool = False,
    enqueue_ts: Optional[float] = None,
    return_mode: Literal["always", "return", "by_bot"] = "by_bot",
    deliver: bool = True,
    settle: bool = True,
    history_context: Optional[str] = None,
    outbound_stream: bool = False,
    stats_chat_type: Optional[str] = None,
    inbox_turn: Optional[InboxTurn] = None,
) -> InteractiveTurnResult:
    """生产与评测共用的一轮编排（H01 之后的分类 / 检索 / 装配 / 结算）。

    调用方只负责建 Event / Session / hook_ctx。分类、检索、CheapGate、结算
    必须走这里，否则评测会变成第三套语义。
    """

    turn = inbox_turn if inbox_turn is not None else single_turn_from_event(event, session.persona_name or "")

    history = get_history_manager()
    # 评测 / HTTP：事件未入 A 轨时合成瞬时记录供线程视图渲染（不落库）。
    render_records: list[MessageRecord] = list(history.get_history(event))
    for entry in turn.entries:
        if history.find_by_seq(event, entry.item.seq) is None:
            seq = history.head_seq(event) + 1
            entry.item.seq = seq
            render_records.append(
                MessageRecord(
                    role="user",
                    content=entry.event.raw_text or "",
                    user_id=str(entry.event.user_id or ""),
                    link=EventLink(seq=seq, msg_id=str(entry.event.msg_id or "")),
                )
            )

    # query：单目标 = 该项正文；多目标 = 各 pending 正文按行拼接。
    if turn.multi_target:
        query = "\n".join(e.event.raw_text or "" for e in turn.entries if (e.event.raw_text or "").strip())
    else:
        query = turn.primary.event.raw_text or ""

    def _count_soft_silence() -> None:
        if not soft_triggered or not event.user_id:
            return
        record_gate_silence(event.session_id, str(event.user_id))

    bot_id = bot.bot_id if bot is not None else ""
    hook_ctx.persona_name = session.persona_name
    hook_ctx.mood_key = str(event.group_id) if event.group_id else str(event.user_id)
    hook_ctx.query = query
    hook_ctx.soft_triggered = soft_triggered

    from gsuid_core.ai_core.outbound import inspect_quote_relation

    is_rep_bot, is_bystander_q, is_stale_q = inspect_quote_relation(event)

    # is_tome 用「本批是否有 hard 唤醒」：auto-@ 的引用了你的话也是 hard，
    # 但语义上不是直接找你。at_bot 用主目标链接上的 at_self（不是事件透传）。
    _primary_link = next(
        (r.link for r in render_records if r.link is not None and r.link.seq == turn.primary.item.seq), None
    )
    turn_graph = build_turn_graph(
        query,
        persona_name=session.persona_name or "",
        is_tome=any(e.item.strength == "hard" for e in turn.entries),
        user_type=str(event.user_type or ("group" if event.group_id else "direct")),
        primary_speaker=str(event.user_id or ""),
        recent=recent_history_texts(session.history),
        soft_triggered=soft_triggered,
        recent_tool_call=has_recent_tool_call(session.history),
        followup_max_len=int(ai_config.get_config("scaffold_followup_max_len").data),
        ambient_max_len=int(ai_config.get_config("scaffold_ambient_max_len").data),
        has_reply=bool(event.reply or event.reply_id),
        at_bot=bool(_primary_link and _primary_link.at_self),
        is_reply_to_bot=is_rep_bot,
        is_bystander_quote=is_bystander_q,
        is_stale_quote=is_stale_q,
    )
    hook_ctx.turn_graph = turn_graph
    # 只拦开口门 / 寻址门。lurk / 低好感区要 rel，不能在 AFTER_SESSION 前判。
    if turn_graph.open_gate is GroupOpenGate.SILENCE or turn_graph.address_gated:
        logger.info(t("log.ai.gscore_group_open_gate_silence"))
        _count_soft_silence()
        return InteractiveTurnResult("", "<SILENCE>", True, False, "", True, hook_ctx, hook_ctx.relationship)

    await fire_hooks(AgentHookPoint.AFTER_SESSION, hook_ctx)
    rel = hook_ctx.relationship
    if rel is None:
        rel = await fetch_relationship(str(event.user_id), bot_id, session.persona_name or "")
        hook_ctx.relationship = rel

    prior_turns, hist_records = prior_user_turns_for_intent(event, query)
    if not prior_turns:
        prior_turns = prior_turns_from_agent_history(session.history, query)
    if not hook_ctx.prior_user_turns:
        hook_ctx.prior_user_turns = prior_turns
    hook_ctx.prev_turn_used_tools = prev_turn_used_tools(session.history, current_user_id=str(event.user_id or ""))
    # 线程视图必须先于 CLASSIFY 渲染：TurnRouter 在 H03 里读 router_view 与 pending 列表。
    delta_text = render_delta(
        render_records,
        from_seq=turn.from_seq,
        head_seq=turn.head_seq,
        backfill=turn.backfill,
        pending_seqs=tuple(e.item.seq for e in turn.entries),
        persona_name=session.persona_name,
        private=not bool(event.group_id),
    )
    hook_ctx.router_view = render_router_view(turn, render_records)
    await fire_hooks(AgentHookPoint.CLASSIFY, hook_ctx)
    intent = hook_ctx.intent or ""

    cheap = decide_cheap_gate(
        turn_graph,
        soft_triggered=soft_triggered,
        intent=intent,
        rel=rel,
        # H03 套件旗 + history 工具证据；装配后的第二道仍用 has_actionable
        has_active_task=(
            hook_ctx.has_actionable or has_recent_tool_call(session.history) or bool(hook_ctx.prev_turn_used_tools)
        ),
    )
    hook_ctx.cheap_gate = cheap.value
    if cheap is CheapGate.SILENCE:
        logger.info(t("log.ai.gscore_group_open_gate_silence"))
        if settle:
            await _settle_silent_turn(event, bot_id, query, intent, rel, hook_ctx.persona_name or "")
        _count_soft_silence()
        return InteractiveTurnResult("", "<SILENCE>", True, False, intent, True, hook_ctx, rel)
    logger.info(t("log.ai.gscore_ai_intent_mode", intent=intent or "-"))

    if soft_triggered:
        hook_ctx.gate_history = history.get_history(event, limit=15)
        decision = await fire_hooks(AgentHookPoint.REACTIVE_GATE, hook_ctx)
        if decision is not HookDecision.CONTINUE or not hook_ctx.should_speak:
            record_gate_silence(event.session_id, str(event.user_id))
            if decision is HookDecision.SILENCE and hook_ctx.decision_reason == "timeout":
                record_gate_outcome("timeout_silence")
            return InteractiveTurnResult("", "<SILENCE>", True, False, intent, True, hook_ctx, rel)
        if enqueue_ts is not None:
            enqueue_ts = time.time()

    await session.refresh_model_if_changed()
    pending_images = []
    if not (event.image_id_list or event.image_id):
        pending_images = extract_pending_unread_images(event, session.read_image_ids)

    # 线程视图 delta 已在 CLASSIFY 前渲染（路由要看同一份时间线），这里直接入 payload。
    user_messages, guard_flags = await prepare_media_payload(
        [(e.item.seq, e.event) for e in turn.entries],
        delta_text,
        session.task_level,
        task_kind=session.task_kind,
        pending_unread_images=pending_images,
    )
    await apply_summary_guard(event, user_messages)
    stamp_current_time(user_messages, now=hook_ctx.clock_at)

    hook_ctx.assembled_domains = session.get_assembled_capability_domains()
    hook_ctx.priority_speakers = await collect_priority_speakers(
        bot_id=bot_id,
        group_id=str(event.group_id) if event.group_id else None,
        history=hist_records,
        current_user_id=str(event.user_id) if event.user_id else None,
        query=query,
        persona_name=session.persona_name,
    )
    await fire_hooks(AgentHookPoint.RETRIEVE_CONTEXT, hook_ctx)
    # 检索结束后重新计时：ledger dedicated 最长 120s，默认 15s。
    if enqueue_ts is not None:
        enqueue_ts = time.time()

    hook_ctx.turn_graph = turn_graph
    hook_ctx.cheap_gate = cheap.value
    session.route_decision = hook_ctx.route_decision
    hook_ctx.recent_report_titles = recent_report_titles(session.history)
    hist_block = ""
    if history_context is not None:
        hist_block = history_context
    elif event.group_id:
        # 群聊增量已在 user 消息的线程视图里；history 块不再拼旧群转录。
        hist_block = ""
    else:
        # 私聊在会话冷启动时（历史为空或首轮），回灌近 8 条近期私聊记录
        is_cold = len(session.history) <= 1
        hist_block = build_private_history_block(event, is_cold_session=is_cold)
    full_context, has_actionable = await assemble_dynamic_context(
        query=query,
        user_id=str(event.user_id),
        bot_id=bot_id,
        persona_name=session.persona_name,
        mood_key=hook_ctx.mood_key,
        group_id=str(event.group_id) if event.group_id else None,
        rel=rel,
        history_context=hist_block,
        soft_triggered=soft_triggered,
        intent=intent,
        recent_report_titles=hook_ctx.recent_report_titles,
        prev_turn_used_tools=hook_ctx.prev_turn_used_tools,
        event=event,
        bot=bot,
        hook_ctx=hook_ctx,
    )
    cheap = decide_cheap_gate(
        turn_graph,
        soft_triggered=soft_triggered,
        has_active_task=has_actionable,
        intent=intent,
        rel=rel,
    )
    if cheap is CheapGate.SILENCE:
        logger.info(t("log.ai.gscore_group_open_gate_silence"))
        if settle:
            await _settle_silent_turn(event, bot_id, query, intent, rel, hook_ctx.persona_name or "")
        _count_soft_silence()
        return InteractiveTurnResult("", "<SILENCE>", True, False, intent, True, hook_ctx, rel)

    from gsuid_core.ai_core.autonomy.dispatch import ack_autonomy_merge, peek_autonomy_merge

    merge_line = peek_autonomy_merge(event, session.persona_name or "")
    if merge_line:
        user_messages = [*user_messages, merge_line]

    turn.reached_model = True
    chat_result = await session.run(
        user_message=user_messages,
        bot=bot,
        ev=event,
        rag_context=full_context,
        return_mode=return_mode,
        enqueue_ts=enqueue_ts,
        intent=intent,
        has_active_task=has_actionable,
        turn_graph=turn_graph,
        cheap_gate=cheap,
        outbound_stream=outbound_stream,
        stats_chat_type=stats_chat_type,
    )

    result_text, is_silence, is_error = classify_run_result(chat_result)
    if merge_line and not is_silence and not is_error:
        ack_autonomy_merge(event, session.persona_name or "")
        from gsuid_core.ai_core.autonomy.micro_arc import load_micro_arcs, save_micro_arcs, consolidate_for_target

        _pn = session.persona_name or ""
        if _pn:
            _arcs = await load_micro_arcs(_pn)
            consolidate_for_target(_arcs, event.group_id or "", event.user_id or "")
            await save_micro_arcs(_pn, _arcs)

    # 收件箱落定：answered 的每人登记续聊窗口；followup 项按 answered/dismissed 记账。
    session.thread_backfill_due = False
    if not is_error:
        _fw = ai_config.get_config("follow_up_window").data
        _fmax = ai_config.get_config("follow_up_max_total").data
        answered_uids = {e.item.user_id for e in turn.entries if e.item.seq in turn.answered}
        for uid in answered_uids:
            note_hard_trigger(event.session_id, str(uid), _fw, _fmax)
        for e in turn.entries:
            if e.item.kind != "followup":
                continue
            if e.item.seq in turn.answered:
                record_soft_reply(event.session_id, str(e.item.user_id))
            else:
                record_gate_silence(event.session_id, str(e.item.user_id))
    if deliver:
        await deliver_run_result(
            bot,
            event,
            chat_result,
            result_text=result_text,
            is_silence=is_silence,
            is_error=is_error,
            intent=intent,
        )

    if settle and session.persona_name:
        hook_ctx.tool_names_called = tuple(session._last_attempt_tool_calls)
        # 入档/蒸馏用的是剥签台词；<reply #n> 只留给出站路由。
        hook_ctx.result_text = strip_protocol_tags(result_text).strip()[:200]
        _think_max = int(ai_config.get_config("thinking_text_max").data)
        _think = session._last_attempt_thinking or ""
        hook_ctx.thinking_text = _think[-_think_max:] if _think else ""
        await _settle_and_fire_after_run(
            bot=bot,
            event=event,
            hook_ctx=hook_ctx,
            bot_id=bot_id,
            query=query,
            intent=intent,
            rel=rel,
            cheap=cheap,
            guard_flags=guard_flags,
            effective=not is_error and (session.last_run_sent_visible_reply or (bool(result_text) and not is_silence)),
            is_silence=is_silence,
            is_error=is_error,
            persona_name=session.persona_name or "",
        )
    return InteractiveTurnResult(chat_result, result_text, is_silence, is_error, intent, False, hook_ctx, rel)


async def _settle_and_fire_after_run(
    *,
    bot: Bot,
    event: Event,
    hook_ctx: AgentHookContext,
    bot_id: str,
    query: str,
    intent: str,
    rel: RelationshipView,
    cheap: CheapGate,
    guard_flags: GuardFlags | None,
    effective: bool,
    is_silence: bool,
    is_error: bool,
    persona_name: str = "",
) -> None:
    """⑩ 关系温度结算 + H08 收尾套件（mood 更新 / 统计上报共用同一份信号扫描）。"""
    outcome = await settle_turn(
        user_id=str(event.user_id),
        bot_id=bot_id,
        user_text=query,
        intent=intent,
        effective=effective,
        silenced=is_silence,
        error=is_error,
        reached_model=True,
        is_master=rel.is_master,
        guard_flags=guard_flags,
        persona_name=persona_name,
    )
    if persona_name:
        from gsuid_core.ai_core.relationship.dyadic_distill import maybe_schedule_dyadic_distill

        await maybe_schedule_dyadic_distill(
            event=event,
            bot=bot,
            user_id=event.user_id,
            bot_id=bot_id,
            persona_name=persona_name,
            score_after=outcome.score_after,
        )
    hook_ctx.point = AgentHookPoint.AFTER_RUN
    hook_ctx.signals = outcome.signals
    hook_ctx.settle_outcome = outcome
    hook_ctx.result_text = query if not hook_ctx.result_text else hook_ctx.result_text
    task = asyncio.create_task(fire_hooks(AgentHookPoint.AFTER_RUN, hook_ctx))

    underlying = _underlying_bot(bot)
    if underlying is not None:
        underlying._add_bg_task(task)
    else:
        logger.warning(t("log.ai.gscore_unable_obtain_bot"))


def _underlying_bot(bot: Bot | _Bot | MockBot) -> _Bot | None:
    """取可挂后台任务的底层 ``_Bot``。先判 ``Bot``，避免 ``Bot`` 继承 ``_Bot`` 时走错支。"""
    if isinstance(bot, Bot):
        return bot.bot
    if isinstance(bot, _Bot):
        return bot
    if isinstance(bot, MockBot) and isinstance(bot._real_bot, Bot):
        return bot._real_bot.bot
    return None


async def _settle_silent_turn(
    event: Event,
    bot_id: str,
    query: str,
    intent: str,
    rel: RelationshipView,
    persona_name: str = "",
) -> None:
    """CheapGate 静音早退路径的结算（``reached_model=False``：只放行负信号）。

    不补这一次结算就会出现吸收态：**用户越界 → zone 掉到 cold → 以后未 @ 的越界发言
    全部走早退 → 再也扣不到分**。闸门应该过滤（只过滤正信号），不该整轮跳过。
    """
    guard = GuardFlags()
    if ai_config.get_config("content_guard_enable").data and event.text:
        _, guard = annotate_untrusted_message_ex(event.text.strip())
    await settle_turn(
        user_id=str(event.user_id),
        bot_id=bot_id,
        user_text=query,
        intent=intent,
        effective=False,
        silenced=True,
        error=False,
        reached_model=False,
        is_master=rel.is_master,
        guard_flags=guard,
        persona_name=persona_name,
    )


# 情绪更新已迁至 ``kits/mood/kit.py``（H08）：六张关键词表在 relationship/signals.py，
# mood 与关系温度一次扫描两用，结论不会再各说各话。

# 模块加载即登记，保证首个 WS 事件到达前 runner 已就位。
set_turn_runner(handle_inbox_turn)
