from server import Bot
from config import core_config
from model import MessageContent, MessageReceive

from gsuid_core.sv import SL

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
    for sv in SL.lst:
        # 服务启动且权限等级超过服务权限
        if SL.lst[sv].enabled and user_pm <= SL.lst[sv].permission:
            for trigger in SL.lst[sv].TL:
                if trigger.check_command(message):
                    message = await trigger.get_command(message)
                    await trigger.func(ws, message)
                    break
            else:
                await ws.send('已收到消息...')
