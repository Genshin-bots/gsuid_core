import asyncio
from copy import deepcopy
from typing import Dict, List

from gsuid_core.sv import SL
from gsuid_core.bot import Bot, _Bot
from gsuid_core.logger import logger
from gsuid_core.trigger import Trigger
from gsuid_core.config import core_config
from gsuid_core.models import Event, Message, MessageReceive

command_start = core_config.get_config('command_start')
config_masters = core_config.get_config('masters')
config_superusers = core_config.get_config('superusers')


async def get_user_pml(msg: MessageReceive) -> int:
    if msg.user_id in config_masters:
        return 0
    elif msg.user_id in config_superusers:
        return 1
    else:
        return msg.user_pm if msg.user_pm >= 1 else 2


async def msg_process(msg: MessageReceive) -> Event:
    if ':' in msg.bot_id:
        bot_id = msg.bot_id.split(':')[0]
    else:
        bot_id = msg.bot_id

    event = Event(
        bot_id,
        msg.bot_self_id,
        msg.msg_id,
        msg.user_type,
        msg.group_id,
        msg.user_id,
        msg.sender,
        msg.user_pm,
        real_bot_id=msg.bot_id,
    )
    _content: List[Message] = []
    for _msg in msg.content:
        if _msg.type == 'text':
            event.raw_text += _msg.data.strip()  # type:ignore
            event.text += _msg.data.strip()  # type:ignore
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
        elif _msg.type == 'file' and _msg.data:
            data = _msg.data.split('|')
            event.file_name = data[0]
            event.file = data[1]
            if str(event.file).startswith(('http', 'https')):
                event.file_type = 'url'
            else:
                event.file_type = 'base64'
        _content.append(_msg)
    event.content = _content
    return event


async def handle_event(ws: _Bot, msg: MessageReceive):
    # 获取用户权限，越小越高
    msg.user_pm = user_pm = await get_user_pml(msg)
    event = await msg_process(msg)
    logger.info('[收到事件]', event=event)

    gid = event.group_id if event.group_id else '0'
    uid = event.user_id if event.user_id else '0'
    uuid = f'{gid}{uid}'
    instances = Bot.get_instances()
    mutiply_instances = Bot.get_mutiply_instances()
    mutiply_map = Bot.get_mutiply_map()

    if uuid in instances and instances[uuid].receive_tag:
        instances[uuid].resp.append(event)
        instances[uuid].set_event()
        return

    if (
        gid in mutiply_map
        and event.user_type != 'direct'
        and mutiply_map[gid] in mutiply_instances
        and mutiply_instances[mutiply_map[gid]].mutiply_tag
    ):
        mutiply_instances[mutiply_map[gid]].mutiply_resp.append(event)
        mutiply_instances[mutiply_map[gid]].set_mutiply_event()
        if uuid == mutiply_instances[mutiply_map[gid]].uuid:
            return

    is_start = False
    if command_start and event.raw_text:
        for start in command_start:
            if event.raw_text.strip().startswith(start):
                event.raw_text = event.raw_text.replace(start, '', 1)
                is_start = True
        else:
            if not is_start:
                return

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
            and msg.group_id not in SL.lst[sv].black_list
            and msg.user_id not in SL.lst[sv].black_list
            and (
                True
                if SL.lst[sv].area == 'ALL'
                or (msg.group_id and SL.lst[sv].area == 'GROUP')
                or (not msg.group_id and SL.lst[sv].area == 'DIRECT')
                else False
            )
            and (
                True
                if (not SL.lst[sv].white_list or SL.lst[sv].white_list == [''])
                else (
                    msg.user_id in SL.lst[sv].white_list
                    or msg.group_id in SL.lst[sv].white_list
                )
            )
        )
    ]
    await asyncio.gather(*pending, return_exceptions=True)
    if len(valid_event) >= 1:
        sorted_event = sorted(valid_event.items(), key=lambda x: x[1])
        for trigger, _ in sorted_event:
            _event = deepcopy(event)
            message = await trigger.get_command(_event)
            bot = Bot(ws, _event)
            logger.info(
                '[命令触发]',
                trigger=[_event.raw_text, trigger.type, trigger.keyword],
            )
            logger.info('[命令触发]', command=message)
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
