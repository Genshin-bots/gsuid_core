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


def _capture_warning(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    warnings: List[str] = []

    def _warning(event: object, *_args: object, **_kwargs: object) -> None:
        warnings.append(str(event))

    monkeypatch.setattr("gsuid_core.handler.logger.warning", _warning)
    return warnings


async def _connected_socket() -> tuple[WebSocket, List[Message]]:
    sent: List[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    ws = WebSocket({"type": "websocket", "path": "/ws/x", "headers": []}, receive, send)
    await ws.accept()
    return ws, sent


async def _send_raises_socket() -> WebSocket:
    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        if message["type"] == "websocket.send":
            raise RuntimeError(_UVICORN_SEND_AFTER_CLOSE)

    ws = WebSocket({"type": "websocket", "path": "/ws/x", "headers": []}, receive, send)
    await ws.accept()
    return ws


@pytest.fixture(autouse=True)
def _restore_gss_state() -> Iterator[None]:
    saved_ws = dict(gss.active_ws)
    saved_bot = dict(gss.active_bot)
    yield
    gss.active_ws.clear()
    gss.active_ws.update(saved_ws)
    gss.active_bot.clear()
    gss.active_bot.update(saved_bot)


def test_undecodable_frame_warns_once_and_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings = _capture_warning(monkeypatch)
    assert decode_inbound_frame(b'{"bot_id":"yunzai"', "yunzai") is None
    assert len(warnings) == 1
    assert "yunzai" in warnings[0]


def test_wrong_type_frame_warns_once_and_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings = _capture_warning(monkeypatch)
    assert decode_inbound_frame(b"[]", "yunzai") is None
    assert len(warnings) == 1
    assert "Expected" in warnings[0]


def test_valid_frame_decodes_without_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings = _capture_warning(monkeypatch)
    raw = msgjson.encode(
        MessageReceive(
            bot_id="yunzai",
            bot_self_id="1",
            user_id="2",
            content=[CoreMessage(type="text", data="hi")],
        )
    )
    decoded = decode_inbound_frame(raw, "yunzai")
    assert decoded is not None
    assert decoded.bot_id == "yunzai"
    assert decoded.content[0].data == "hi"
    assert warnings == []


def test_gs_logger_delivers_when_connected() -> None:
    async def scenario() -> None:
        ws, sent = await _connected_socket()
        await GsLogger(BOT_ID, ws).info("hello")
        assert [m["type"] for m in sent] == ["websocket.accept", "websocket.send"]

    asyncio.run(scenario())


def test_gs_logger_silent_after_close() -> None:
    async def scenario() -> None:
        ws, sent = await _connected_socket()
        glog = GsLogger(BOT_ID, ws)
        await ws.close(code=1001)
        await glog.info("after close")
        assert ws.application_state == WebSocketState.DISCONNECTED
        assert not any(m["type"] == "websocket.send" for m in sent)

    asyncio.run(scenario())


def test_gs_logger_swallows_send_race() -> None:
    async def scenario() -> None:
        ws = await _send_raises_socket()
        glog = GsLogger(BOT_ID, ws)
        await glog.info("raced")
        assert glog.bot is None
        await glog.info("raced again")

    asyncio.run(scenario())


def test_stale_disconnect_leaves_new_connection_alone() -> None:
    async def scenario() -> None:
        stale_ws, _ = await _connected_socket()
        fresh_ws, fresh_sent = await _connected_socket()
        bot = _Bot(BOT_ID, fresh_ws)
        gss.active_ws[BOT_ID] = fresh_ws
        gss.active_bot[BOT_ID] = bot

        await gss.disconnect(BOT_ID, stale_ws)

        assert gss.active_ws.get(BOT_ID) is fresh_ws
        assert bot.bot is fresh_ws
        assert fresh_ws.application_state == WebSocketState.CONNECTED
        assert not any(m["type"] == "websocket.close" for m in fresh_sent)

    asyncio.run(scenario())


def test_current_disconnect_tears_down() -> None:
    async def scenario() -> None:
        ws, sent = await _connected_socket()
        bot = _Bot(BOT_ID, ws)
        gss.active_ws[BOT_ID] = ws
        gss.active_bot[BOT_ID] = bot

        await gss.disconnect(BOT_ID, ws)

        assert BOT_ID not in gss.active_ws
        assert bot.bot is None
        assert any(m["type"] == "websocket.close" for m in sent)

    asyncio.run(scenario())
