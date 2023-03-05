import asyncio
from typing import List, Union, Literal, Optional

from fastapi import WebSocket
from msgspec import json as msgjson

from gsuid_core.logger import logger
from gsuid_core.gs_logger import GsLogger
from gsuid_core.segment import MessageSegment
from gsuid_core.models import Event, Message, MessageSend


class _Bot:
    def __init__(self, _id: str, ws: WebSocket):
        self.bot_id = _id
        self.bot = ws
        self.logger = GsLogger(self.bot_id, ws)
        self.queue = asyncio.queues.Queue()
        self.bg_tasks = set()

    async def target_send(
        self,
        message: Union[Message, List[Message], str, bytes],
        target_type: Literal['group', 'direct', 'channel', 'sub_channel'],
        target_id: Optional[str],
        bot_id: str,
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
        send = MessageSend(
            content=message,
            bot_id=bot_id,
            target_type=target_type,
            target_id=target_id,
        )
        logger.info(f'[发送消息to] {target_id}')
        await self.bot.send_bytes(msgjson.encode(send))

    async def _process(self):
        while True:
            data = await self.queue.get()
            task = asyncio.create_task(data)
            self.bg_tasks.add(task)
            task.add_done_callback(lambda _: self.bg_tasks.discard(task))


class Bot:
    def __init__(self, bot: _Bot, ev: Event):
        self.bot = bot
        self.ev = ev
        self.logger = self.bot.logger
        self.bot_id = self.bot.bot_id

    async def send(self, message: Union[Message, List[Message], str, bytes]):
        return await self.bot.target_send(
            message,
            self.ev.user_type,
            self.ev.group_id if self.ev.group_id else self.ev.user_id,
            self.ev.bot_id,
        )

    async def target_send(
        self,
        message: Union[Message, List[Message], str, bytes],
        target_type: Literal['group', 'direct', 'channel', 'sub_channel'],
        target_id: Optional[str],
    ):
        return await self.bot.target_send(
            message, target_type, target_id, self.ev.bot_id
        )
