from typing import Literal, Optional

from fastapi import WebSocket
from msgspec import json as msgjson
from starlette.websockets import WebSocketState, WebSocketDisconnect, WebSocketDisconnected

from gsuid_core.models import MessageSend
from gsuid_core.segment import MessageSegment


class GsLogger:
    def __init__(self, bot_id: str, ws: Optional[WebSocket]):
        self.bot_id = bot_id
        self.bot = ws

    def get_msg_send(self, type: Literal["INFO", "WARNING", "ERROR", "SUCCESS"], msg: str):
        return MessageSend(
            content=[MessageSegment.log(type, msg)],
            bot_id=self.bot_id,
            target_type=None,
            target_id=None,
        )

    async def _send(self, payload: bytes) -> None:
        # 套接字已关闭时丢弃日志。推送失败不应打断调用方。
        socket = self.bot
        if socket is None or socket.application_state != WebSocketState.CONNECTED:
            return
        try:
            await socket.send_bytes(payload)
        except (WebSocketDisconnect, WebSocketDisconnected):
            self.bot = None
        except RuntimeError as exc:
            # uvicorn 在应用状态仍为 CONNECTED 时抛出这条错误。
            if "Unexpected ASGI message" not in str(exc):
                raise
            self.bot = None

    async def info(self, msg: str):
        await self._send(msgjson.encode(self.get_msg_send("INFO", msg)))

    async def warning(self, msg: str):
        await self._send(msgjson.encode(self.get_msg_send("WARNING", msg)))

    async def error(self, msg: str):
        await self._send(msgjson.encode(self.get_msg_send("ERROR", msg)))

    async def success(self, msg: str):
        await self._send(msgjson.encode(self.get_msg_send("SUCCESS", msg)))
