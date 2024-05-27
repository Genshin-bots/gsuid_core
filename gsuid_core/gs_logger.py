from typing import Literal, Optional

from fastapi import WebSocket
from msgspec import json as msgjson

from gsuid_core.models import MessageSend
from gsuid_core.segment import MessageSegment


class GsLogger:
    def __init__(self, bot_id: str, ws: Optional[WebSocket]):
        self.bot_id = bot_id
        self.bot = ws

    def get_msg_send(
        self, type: Literal['INFO', 'WARNING', 'ERROR', 'SUCCESS'], msg: str
    ):
        return MessageSend(
            content=[MessageSegment.log(type, msg)],
            bot_id=self.bot_id,
            target_type=None,
            target_id=None,
        )

    async def _send(self, b: bytes):
        if self.bot:
            await self.bot.send_bytes(b)

    async def info(self, msg: str):
        await self._send(msgjson.encode(self.get_msg_send('INFO', msg)))

    async def warning(self, msg: str):
        await self._send(msgjson.encode(self.get_msg_send('WARNING', msg)))

    async def error(self, msg: str):
        await self._send(msgjson.encode(self.get_msg_send('ERROR', msg)))

    async def success(self, msg: str):
        await self._send(msgjson.encode(self.get_msg_send('SUCCESS', msg)))
