import asyncio

from server import Bot
from trigger import Trigger
from config import core_config

from gsuid_core.sv import SL
from gsuid_core.models import MessageContent, MessageReceive

config_masters = core_config.get_config('masters')
config_superusers = core_config.get_config('superusers')


async def get_user_pml(msg: MessageReceive) -> int:
    if msg.user_id in config_masters:
        return 0
    elif msg.user_id in config_superusers:
        return 1
    else:
        return msg.user_pm


async def msg_process(msg: MessageReceive) -> MessageContent:
    message = MessageContent(raw=msg)
    for _msg in msg.content:
        if _msg.type == 'text':
            message.raw_text = _msg.data  # type:ignore
        elif _msg.type == 'at':
            message.at = _msg.data
            message.at_list.append(_msg.data)
        elif _msg.type == 'image':
            message.image = _msg.data
            message.image_list.append(_msg.data)
    return message


async def handle_event(ws: Bot, msg: MessageReceive):
    # 获取用户权限，越小越高
    user_pm = await get_user_pml(msg)
    message = await msg_process(msg)
    ws.user_id = msg.user_id
    ws.group_id = msg.group_id
    ws.user_type = msg.user_type
    print(f'[收到消息] {msg}')
    pending = [
        _check_command(ws, SL.lst[sv].TL[tr], message)
        for sv in SL.lst
        for tr in SL.lst[sv].TL
        if (
            SL.lst[sv].enabled
            and user_pm <= SL.lst[sv].permission
            and msg.group_id not in SL.lst[sv].black_list
        )
    ]
    await asyncio.gather(*pending, return_exceptions=True)


async def _check_command(ws: Bot, trigger: Trigger, message: MessageContent):
    if trigger.check_command(message):
        message = await trigger.get_command(message)
        ws.queue.put_nowait(trigger.func(ws, message))
