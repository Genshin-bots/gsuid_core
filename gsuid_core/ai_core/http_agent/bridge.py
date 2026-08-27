"""开流后装配：A 轨 + H00 + ``run_passive_interactive_chat``。禁止 ``handle_event``。"""

from __future__ import annotations

import asyncio

from gsuid_core.bot import _Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.handle_ai import PassiveChatResult, run_passive_interactive_chat
from gsuid_core.ai_core.http_agent.types import HTTP_AGENT_WS_BOT_ID, HTTP_STATS_CHAT_TYPE
from gsuid_core.ai_core.http_agent.runtime import bind_agent
from gsuid_core.ai_core.http_agent.capture_bot import CaptureBot, CaptureItem


async def attach_inbound_tracks(bot: CaptureBot, event: Event) -> None:
    """A 轨（纯图也记）+ H00。禁止再手写 observe()。"""
    from gsuid_core.ai_core.hooks import AgentHookPoint, AgentHookContext, fire_hooks, should_fire
    from gsuid_core.message_history import get_history_manager

    user_name = None
    user_avatar = None
    if event.sender and "nickname" in event.sender:
        user_name = event.sender["nickname"]
    if event.sender and "avatar" in event.sender:
        user_avatar = event.sender["avatar"]
    metadata: dict[str, object] = {
        "msg_id": event.msg_id,
        "bot_id": event.bot_id,
        "user_type": event.user_type,
    }
    if event.image_id_list:
        metadata["image_id_list"] = event.image_id_list
    elif event.image_id:
        metadata["image_id"] = event.image_id
    get_history_manager().add_message(
        event=event,
        role="user",
        content=event.raw_text.strip() if event.raw_text else "",
        user_name=user_name if isinstance(user_name, str) else None,
        user_avatar=user_avatar if isinstance(user_avatar, str) else None,
        metadata=metadata,
    )
    if should_fire(AgentHookPoint.ON_INBOUND):
        inbound_ctx = AgentHookContext(
            point=AgentHookPoint.ON_INBOUND,
            ev=event,
            session_id=event.session_id,
            create_by="Chat",
            query=event.raw_text or "",
        )
        bot.bot._add_bg_task(asyncio.create_task(fire_hooks(AgentHookPoint.ON_INBOUND, inbound_ctx)))


def make_capture_bot(event: Event, queue: asyncio.Queue[CaptureItem]) -> CaptureBot:
    return CaptureBot(_Bot(HTTP_AGENT_WS_BOT_ID), event, queue)


async def run_http_agent_turn(
    *,
    bot: CaptureBot,
    event: Event,
    wall_clock: int,
    run_id: str,
) -> PassiveChatResult:
    from gsuid_core.ai_core.ai_router import get_ai_session

    await attach_inbound_tracks(bot, event)
    # 首轮 registry 里还没有 session；先创建再 bind，cancel 才能 set _cancel_generation
    session = await get_ai_session(event)
    bind_agent(run_id, session)
    return await run_passive_interactive_chat(
        bot,
        event,
        enqueue_ts=None,
        budget_mode="skip",
        wall_clock_budget=float(wall_clock),
        return_mode="by_bot",
        deliver=True,
        outbound_stream=True,
        stats_chat_type=HTTP_STATS_CHAT_TYPE,
    )
