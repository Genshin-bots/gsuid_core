"""AI 聊天入口：**薄编排器**。

本模块只做编排——并发闸 / TTL / 预算 / Session / 长度防护 / 开口门 / 结算调用 /
出站分发，以及在固定锚点 ``fire_hooks``。产品能力（记忆、关系温度、情绪、意图分类、
软触发门、脚手架注入、工具装配）全部是**套件**，挂在 hook 总线上。

``run_interactive_turn`` 是生产 ``handle_ai_chat`` 与评测
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
from gsuid_core.ai_core.utils import prepare_content_payload, has_model_visible_content
from gsuid_core.ai_core.gs_agent import STALE_CHAT_REQUEST_TTL, GsCoreAIAgent
from gsuid_core.ai_core.ai_router import get_ai_session
from gsuid_core.ai_core.relationship import RelationshipView, fetch_relationship, collect_priority_speakers
from gsuid_core.ai_core.content_guard import GuardFlags
from gsuid_core.ai_core.turn_pipeline import (
    stale_request,
    check_budget_gate,
    deliver_run_result,
    stamp_current_time,
    apply_summary_guard,
    classify_run_result,
    prev_turn_used_tools,
    recent_report_titles,
    build_group_history_block,
    apply_absolute_length_guard,
    prior_user_turns_for_intent,
    prior_turns_from_agent_history,
)
from gsuid_core.ai_core.trigger_bridge import MockBot
from gsuid_core.ai_core.context_assembly import assemble_dynamic_context
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.relationship.engine import settle_turn
from gsuid_core.ai_core.interaction_scaffold import CheapGate, GroupOpenGate

# AI并发控制配置
MAX_CONCURRENT_AI_CALLS = 10  # 全局最大并发AI调用数
_ai_semaphore = asyncio.Semaphore(MAX_CONCURRENT_AI_CALLS)  # AI并发信号量


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
        from gsuid_core.ai_core.startup import is_ai_core_ready, wait_ai_core_ready

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
        from gsuid_core.ai_core.turn_pipeline import evaluate_budget

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

    session = await get_ai_session(event)
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
    )
    if turn.is_silence:
        return PassiveChatResult("silence", turn=turn)
    return PassiveChatResult("ok", turn=turn)


async def handle_ai_chat(
    bot: Bot,
    event: Event,
    enqueue_ts: Optional[float] = None,
    soft_triggered: bool = False,
) -> None:
    """被动交互主链路（异步队列执行）。

    锚点顺序：预算 → H01 → 长度防护 → Session → TurnGraph/开口门 → H02 → H03 分类
    → 开口门重判 → H04 软门 → payload → H05 检索 → H06/H07 合成 → 开口门终判
    → ``session.run`` → 出站 → 结算 + H08。

    Args:
        bot: Bot对象，用于发送消息
        event: Event事件对象，包含用户输入和相关上下文
        enqueue_ts: 入队时刻（O-A 队头阻塞防护）
        soft_triggered: 是否为免唤醒续聊软触发
    """
    if not ai_config.get_config("enable").data:
        logger.debug(t("log.ai.gscore_service_enabled_skipping"))
        return
    if not await _wait_core_ready():
        return

    async with _ai_semaphore:
        if stale_request(enqueue_ts, STALE_CHAT_REQUEST_TTL):
            return
        try:
            await run_passive_interactive_chat(
                bot,
                event,
                enqueue_ts=enqueue_ts,
                soft_triggered=soft_triggered,
            )
        except Exception as e:
            logger.exception(t("log.ai.gscore_ai_exception_chat_error", e=e))


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
) -> InteractiveTurnResult:
    """生产与评测共用的一轮编排（H01 之后的分类 / 检索 / 装配 / 结算）。

    调用方只负责建 Event / Session / hook_ctx。分类、检索、CheapGate、结算
    必须走这里，否则评测会变成第三套语义。
    """
    from gsuid_core.ai_core.interaction_scaffold import (
        build_turn_graph,
        decide_cheap_gate,
        has_recent_tool_call,
        recent_history_texts,
    )

    bot_id = bot.bot_id if bot is not None else ""
    hook_ctx.persona_name = session.persona_name
    hook_ctx.mood_key = str(event.group_id) if event.group_id else str(event.user_id)
    hook_ctx.query = query
    hook_ctx.soft_triggered = soft_triggered

    turn_graph = build_turn_graph(
        query,
        persona_name=session.persona_name or "",
        is_tome=bool(event.is_tome),
        user_type=str(event.user_type or ("group" if event.group_id else "direct")),
        primary_speaker=str(event.user_id or ""),
        recent=recent_history_texts(session.history),
        soft_triggered=soft_triggered,
        recent_tool_call=has_recent_tool_call(session.history),
        followup_max_len=int(ai_config.get_config("scaffold_followup_max_len").data),
        ambient_max_len=int(ai_config.get_config("scaffold_ambient_max_len").data),
        has_reply=bool(event.reply or event.reply_id),
    )
    hook_ctx.turn_graph = turn_graph
    # 只拦开口门 / 寻址门。lurk / 低好感区要 rel，不能在 AFTER_SESSION 前判。
    if turn_graph.open_gate is GroupOpenGate.SILENCE or turn_graph.address_gated:
        logger.info(t("log.ai.gscore_group_open_gate_silence"))
        return InteractiveTurnResult("", "<SILENCE>", True, False, "", True, hook_ctx, hook_ctx.relationship)

    await fire_hooks(AgentHookPoint.AFTER_SESSION, hook_ctx)
    rel = hook_ctx.relationship
    if rel is None:
        rel = await fetch_relationship(str(event.user_id), bot_id)
        hook_ctx.relationship = rel

    prior_turns, hist_records = prior_user_turns_for_intent(event, query)
    if not prior_turns:
        prior_turns = prior_turns_from_agent_history(session.history, query)
    if not hook_ctx.prior_user_turns:
        hook_ctx.prior_user_turns = prior_turns
    hook_ctx.prev_turn_used_tools = prev_turn_used_tools(session.history)
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
            await _settle_silent_turn(event, bot_id, query, intent, rel)
        return InteractiveTurnResult("", "<SILENCE>", True, False, intent, True, hook_ctx, rel)
    logger.info(t("log.ai.gscore_ai_intent_mode", intent=intent or "-"))

    if soft_triggered:
        from gsuid_core.message_history import get_history_manager

        hook_ctx.gate_history = get_history_manager().get_history(event, limit=15)
        if await fire_hooks(AgentHookPoint.REACTIVE_GATE, hook_ctx) is not HookDecision.CONTINUE:
            return InteractiveTurnResult("", "<SILENCE>", True, False, intent, True, hook_ctx, rel)
        if not hook_ctx.should_speak:
            return InteractiveTurnResult("", "<SILENCE>", True, False, intent, True, hook_ctx, rel)
        if enqueue_ts is not None:
            enqueue_ts = time.time()

    user_messages, guard_flags = await prepare_content_payload(event, quoted_tome=turn_graph.quoted_tome)
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
    # 检索预算最长 15s，超过队头 TTL；检索结束后重新计时，避免刚查完就被当过期丢弃。
    if enqueue_ts is not None:
        enqueue_ts = time.time()

    hook_ctx.turn_graph = turn_graph
    hook_ctx.cheap_gate = cheap.value
    hook_ctx.recent_report_titles = recent_report_titles(session.history)
    hist_block = ""
    if history_context is not None:
        hist_block = history_context
    elif cheap is CheapGate.FULL and (
        turn_graph.call_to_self or turn_graph.ellipsis_followup or turn_graph.task_management
    ):
        hist_block = build_group_history_block(event)
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
            await _settle_silent_turn(event, bot_id, query, intent, rel)
        return InteractiveTurnResult("", "<SILENCE>", True, False, intent, True, hook_ctx, rel)

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
        hook_ctx.result_text = result_text[:200]
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
        is_light=cheap is CheapGate.LIGHT,
        is_master=rel.is_master,
        guard_flags=guard_flags,
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
) -> None:
    """CheapGate 静音早退路径的结算（``reached_model=False``：只放行负信号）。

    不补这一次结算就会出现吸收态：**用户越界 → zone 掉到 cold → 以后未 @ 的越界发言
    全部走早退 → 再也扣不到分**。闸门应该过滤（只过滤正信号），不该整轮跳过。
    """
    from gsuid_core.ai_core.content_guard import GuardFlags, annotate_untrusted_message_ex

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
    )


# 情绪更新已迁至 ``kits/mood/kit.py``（H08）：六张关键词表在 relationship/signals.py，
# mood 与关系温度一次扫描两用，结论不会再各说各话。
