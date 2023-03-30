import asyncio
from typing import Dict, List

from gsuid_core.sv import SL
from gsuid_core.bot import Bot, _Bot
from gsuid_core.logger import logger
from gsuid_core.trigger import Trigger
from gsuid_core.config import core_config
from gsuid_core.models import Event, Message, MessageReceive

config_masters = core_config.get_config('masters')
config_superusers = core_config.get_config('superusers')


async def get_user_pml(msg: MessageReceive) -> int:
    if msg.user_id in config_masters:
        return 0
    elif msg.user_id in config_superusers:
        return 1
    else:
        return msg.user_pm


async def msg_process(msg: MessageReceive) -> Event:
    event = Event(
        msg.bot_id,
        msg.bot_self_id,
        msg.msg_id,
        msg.user_type,
        msg.group_id,
        msg.user_id,
        msg.user_pm,
    )
    _content: List[Message] = []
    for _msg in msg.content:
        if _msg.type == 'text':
            event.raw_text += _msg.data.strip()  # type:ignore
        elif _msg.type == 'at':
            if event.bot_self_id == _msg.data:
                event.is_tome = True
                continue
            else:
                event.at = _msg.data
                event.at_list.append(_msg.data)
        elif _msg.type == 'image':
            event.image = _msg.data
            event.image_list.append(_msg.data)
        elif _msg.type == 'reply':
            event.reply = _msg.data
        _content.append(_msg)
    event.content = _content
    return event


async def handle_event(ws: _Bot, msg: MessageReceive):
    # 获取用户权限，越小越高
    user_pm = await get_user_pml(msg)
    event = await msg_process(msg)
    logger.info(f'[收到事件] {event}')
    valid_event: Dict[Trigger, int] = {}
    pending = [
        _check_command(
            SL.lst[sv].TL[tr],
            SL.lst[sv].priority,
            event,
            valid_event,
        )
        for sv in SL.lst
        for tr in SL.lst[sv].TL
        if (
            SL.lst[sv].enabled
            and user_pm <= SL.lst[sv].pm
            and (
                msg.group_id not in SL.lst[sv].black_list
                or msg.user_id not in SL.lst[sv].black_list
            )
            and True
            if SL.lst[sv].area == 'ALL'
            or (msg.group_id and SL.lst[sv].area == 'GROUP')
            or (not msg.group_id and SL.lst[sv].area == 'DIRECT')
            else False
        )
    ]
    await asyncio.gather(*pending, return_exceptions=True)
    if len(valid_event) >= 1:
        sorted_event = sorted(valid_event.items(), key=lambda x: x[1])
        for trigger, _ in sorted_event:
            message = await trigger.get_command(event)
            bot = Bot(ws, event)
            logger.info(
                f'↪ 消息 「{event.raw_text}」 触发'
                f' 「{trigger.type}」 类型触发器, 关键词:'
                f' 「{trigger.keyword}」 '
            )
            ws.queue.put_nowait(trigger.func(bot, message))
            if trigger.block:
                break


async def _check_command(
    trigger: Trigger,
    priority: int,
    message: Event,
    valid_event: Dict[Trigger, int],
):
    if trigger.check_command(message):
        valid_event[trigger] = priority
