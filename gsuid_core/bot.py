import random
import asyncio
from typing import Dict, List, Union, Literal, Optional

from fastapi import WebSocket
from msgspec import json as msgjson

from gsuid_core.logger import logger
from gsuid_core.gs_logger import GsLogger
from gsuid_core.segment import MessageSegment
from gsuid_core.utils.image.convert import text2pic
from gsuid_core.models import Event, Message, MessageSend
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

R_enabled = core_plugins_config.get_config('AutoAddRandomText').data
R_text = core_plugins_config.get_config('RandomText').data
is_text2pic = core_plugins_config.get_config('AutoTextToPic').data
text2pic_limit = core_plugins_config.get_config('TextToPicThreshold').data
is_specific_msg_id = core_plugins_config.get_config('EnableSpecificMsgId').data
specific_msg_id = core_plugins_config.get_config('SpecificMsgId').data


class _Bot:
    def __init__(self, _id: str, ws: WebSocket):
        self.bot_id = _id
        self.bot = ws
        self.logger = GsLogger(self.bot_id, ws)
        self.queue = asyncio.queues.Queue()
        self.bg_tasks = set()

    async def target_send(
        self,
        message: Union[Message, List[Message], List[str], str, bytes],
        target_type: Literal['group', 'direct', 'channel', 'sub_channel'],
        target_id: Optional[str],
        bot_id: str,
        bot_self_id: str,
        msg_id: str = '',
        at_sender: bool = False,
        sender_id: str = '',
    ):
        if isinstance(message, Message):
            message = [message]
        elif isinstance(message, str):
            if message.startswith('base64://'):
                message = [MessageSegment.image(message)]
            else:
                message = [MessageSegment.text(message)]
        elif isinstance(message, bytes):
            message = [MessageSegment.image(message)]
        elif isinstance(message, List):
            if all(isinstance(x, str) for x in message):
                message = [MessageSegment.node(message)]
        else:
            message = [message]

        _message: List[Message] = message  # type: ignore

        if at_sender and sender_id:
            _message.append(MessageSegment.at(sender_id))

        if R_enabled:
            result = ''.join(
                random.choice(R_text)
                for _ in range(random.randint(1, len(R_text)))
            )
            _message.append(MessageSegment.text(result))

        if is_text2pic:
            if (
                len(_message) == 1
                and _message[0].type == 'text'
                and isinstance(_message[0].data, str)
                and len(_message[0].data) >= int(text2pic_limit)
            ):
                img = await text2pic(_message[0].data)
                _message = [MessageSegment.image(img)]

        if is_specific_msg_id and not msg_id:
            msg_id = specific_msg_id

        send = MessageSend(
            content=_message,
            bot_id=bot_id,
            bot_self_id=bot_self_id,
            target_type=target_type,
            target_id=target_id,
            msg_id=msg_id,
        )
        logger.info(f'[发送消息to] {bot_id} - {target_type} - {target_id}')
        await self.bot.send_bytes(msgjson.encode(send))

    async def _process(self):
        while True:
            data = await self.queue.get()
            asyncio.create_task(data)
            self.queue.task_done()


class Bot:
    instances: Dict[str, "Bot"] = {}

    def __init__(self, bot: _Bot, ev: Event):
        gid = ev.group_id if ev.group_id else 0
        uid = ev.user_id if ev.user_id else 0
        self.uuid = f'{uid}{gid}'
        self.instances[self.uuid] = self

        self.bot = bot
        self.ev = ev
        self.logger = self.bot.logger
        self.bot_id = ev.bot_id
        self.bot_self_id = ev.bot_self_id
        self.event = asyncio.Event()
        self.resp: List[Event] = []

    @classmethod
    def get_instances(cls):
        return cls.instances

    async def wait_for_key(self, timeout: float) -> Optional[Event]:
        await asyncio.wait_for(self.event.wait(), timeout=timeout)

        if self.resp:
            return self.resp[-1]

    def set_event(self):
        self.event.set()

    async def receive_resp(self, timeout: float = 60) -> Optional[Event]:
        return await self.wait_for_key(timeout)

    async def send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
    ):
        return await self.bot.target_send(
            message,
            self.ev.user_type,
            self.ev.group_id if self.ev.group_id else self.ev.user_id,
            self.ev.bot_id,
            self.bot_self_id,
            self.ev.msg_id,
            at_sender,
            self.ev.user_id,
        )

    async def target_send(
        self,
        message: Union[Message, List[Message], str, bytes, List[str]],
        target_type: Literal['group', 'direct', 'channel', 'sub_channel'],
        target_id: Optional[str],
        at_sender: bool = False,
        sender_id: str = '',
    ):
        return await self.bot.target_send(
            message,
            target_type,
            target_id,
            self.ev.bot_id,
            self.ev.bot_self_id,
            self.ev.msg_id,
            at_sender,
            sender_id,
        )
