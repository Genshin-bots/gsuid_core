"""WS 帧解析与断连生命周期安全测试。"""

from __future__ import annotations

import asyncio
from typing import List
from collections.abc import Iterator

import pytest
from msgspec import json as msgjson
from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketState

from gsuid_core.bot import _Bot
from gsuid_core.gss import gss
from gsuid_core.models import Message as CoreMessage, MessageReceive
from gsuid_core.handler import decode_inbound_frame
from gsuid_core.gs_logger import GsLogger

BOT_ID = "test-ws-bot"
_UVICORN_SEND_AFTER_CLOSE = "Unexpected ASGI message 'websocket.send', after sending 'websocket.close'"


async def _connected_socket(*, fail_send: bool = False) -> tuple[WebSocket, List[Message]]:
    sent: List[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        if fail_send and message["type"] == "websocket.send":
            raise RuntimeError(_UVICORN_SEND_AFTER_CLOSE)
        sent.append(message)

    ws = WebSocket({"type": "websocket", "path": "/ws/x", "headers": []}, receive, send)
    await ws.accept()
    return ws, sent


@pytest.fixture(autouse=True)
def _restore_gss_state() -> Iterator[None]:
    saved_ws, saved_bot = dict(gss.active_ws), dict(gss.active_bot)
    yield
    gss.active_ws.clear()
    gss.active_ws.update(saved_ws)
    gss.active_bot.clear()
    gss.active_bot.update(saved_bot)


def test_decode_inbound_frame_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: List[str] = []
    monkeypatch.setattr("gsuid_core.handler.logger.warning", lambda event, *a, **kw: warnings.append(str(event)))

    assert decode_inbound_frame(b'{"bot_id":"yunzai"', "yunzai") is None
    assert len(warnings) == 1 and "yunzai" in warnings[0]

    warnings.clear()
    assert decode_inbound_frame(b"[]", "yunzai") is None
    assert len(warnings) == 1 and "Expected" in warnings[0]

    warnings.clear()
    raw = msgjson.encode(
        MessageReceive(bot_id="yunzai", bot_self_id="1", user_id="2", content=[CoreMessage(type="text", data="hi")])
    )
    decoded = decode_inbound_frame(raw, "yunzai")
    assert decoded is not None and decoded.bot_id == "yunzai" and decoded.content[0].data == "hi"
    assert warnings == []


def test_gs_logger_and_disconnect_lifecycle() -> None:
    async def scenario() -> None:
        ws, sent = await _connected_socket()
        glog = GsLogger(BOT_ID, ws)
        await glog.info("hello")
        assert [m["type"] for m in sent] == ["websocket.accept", "websocket.send"]

        await ws.close(code=1001)
        sent.clear()
        await glog.info("after close")
        assert ws.application_state == WebSocketState.DISCONNECTED and sent == []

        raced_ws, _ = await _connected_socket(fail_send=True)
        raced_log = GsLogger(BOT_ID, raced_ws)
        await raced_log.info("raced")
        assert raced_log.bot is None
        await raced_log.info("raced again")

        stale_ws, _ = await _connected_socket()
        fresh_ws, fresh_sent = await _connected_socket()
        bot = _Bot(BOT_ID, fresh_ws)
        gss.active_ws[BOT_ID] = fresh_ws
        gss.active_bot[BOT_ID] = bot

        await gss.disconnect(BOT_ID, stale_ws)
        assert gss.active_ws.get(BOT_ID) is fresh_ws and bot.bot is fresh_ws
        assert not any(m["type"] == "websocket.close" for m in fresh_sent)

        await gss.disconnect(BOT_ID, fresh_ws)
        assert BOT_ID not in gss.active_ws and bot.bot is None
        assert any(m["type"] == "websocket.close" for m in fresh_sent)

    asyncio.run(scenario())
