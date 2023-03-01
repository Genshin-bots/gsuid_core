import asyncio

from gsuid_core.sv import SL
from gsuid_core.bot import Bot, _Bot
from gsuid_core.trigger import Trigger
from gsuid_core.config import core_config
from gsuid_core.models import Event, MessageReceive

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
        msg.user_type,
        msg.group_id,
        msg.user_id,
        msg.user_pm,
        msg.content,
    )
    for _msg in msg.content:
        if _msg.type == 'text':
            event.raw_text = _msg.data  # type:ignore
        elif _msg.type == 'at':
            event.at = _msg.data
            event.at_list.append(_msg.data)
        elif _msg.type == 'image':
            event.image = _msg.data
            event.image_list.append(_msg.data)
    return event


async def handle_event(ws: _Bot, msg: MessageReceive):
    # 获取用户权限，越小越高
    user_pm = await get_user_pml(msg)
    event = await msg_process(msg)
    bot = Bot(ws, event)
    print(f'[收到消息] {msg}')
    pending = [
        _check_command(ws, bot, SL.lst[sv].TL[tr], event)
        for sv in SL.lst
        for tr in SL.lst[sv].TL
        if (
            SL.lst[sv].enabled
            and user_pm <= SL.lst[sv].pm
            and msg.group_id not in SL.lst[sv].black_list
            and True
            if SL.lst[sv].area == 'ALL'
            or (msg.group_id and SL.lst[sv].area == 'GROUP')
            or (not msg.group_id and SL.lst[sv].area == 'DIRECT')
            else False
        )
    ]
    await asyncio.gather(*pending, return_exceptions=True)


async def _check_command(ws: _Bot, Bot: Bot, trigger: Trigger, message: Event):
    if trigger.check_command(message):
        message = await trigger.get_command(message)
        ws.queue.put_nowait(trigger.func(Bot, message))
