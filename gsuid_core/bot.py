import asyncio
from typing import List, Union, Optional

from logger import logger
from fastapi import WebSocket
from gs_logger import GsLogger
from segment import MessageSegment
from msgspec import json as msgjson
from models import Message, MessageSend


class Bot:
    def __init__(self, _id: str, ws: WebSocket):
        self.bot_id = _id
        self.bot = ws
        self.logger = GsLogger(self.bot_id, ws)
        self.queue = asyncio.queues.Queue()
        self.background_tasks = set()
        self.user_id: Optional[str] = None
        self.group_id: Optional[str] = None
        self.user_type: Optional[str] = None

    async def send(self, message: Union[Message, List[Message], str, bytes]):
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
            bot_id=self.bot_id,
            target_type=self.user_type,
            target_id=self.group_id if self.group_id else self.user_id,
        )
        logger.info(f'[发送消息] {send}')
        await self.bot.send_bytes(msgjson.encode(send))

    async def _process(self):
        while True:
            data = await self.queue.get()
            task = asyncio.create_task(data)
            self.background_tasks.add(task)
            task.add_done_callback(
                lambda _: self.background_tasks.discard(task)
            )
