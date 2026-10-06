"""WS 断开安全性：断连信号不得逃逸成未捕获异常，陈旧连接的收尾不得拆掉新连接。

对应 issue #284（断开 WS 刷整屏 WebSocketDisconnected 堆栈）。
"""

from __future__ import annotations

import asyncio
from typing import List
from collections.abc import Iterator

import pytest
from starlette.types import Message
from starlette.websockets import WebSocket, WebSocketState, WebSocketDisconnected

from gsuid_core.bot import Bot, _Bot
from gsuid_core.gss import gss
from gsuid_core.models import Event
from gsuid_core.gs_logger import GsLogger

BOT_ID = "test-ws-bot"

# uvicorn 在 closed_event 置位、starlette 仍认为 CONNECTED 时对 websocket.send 抛的错
_UVICORN_SEND_AFTER_CLOSE = "Unexpected ASGI message 'websocket.send', after sending 'websocket.close'"


async def _connected_socket() -> tuple[WebSocket, List[Message]]:
    """造一条已 accept 的真实 starlette WebSocket，收发走 fake ASGI 通道。"""
    sent: List[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    ws = WebSocket({"type": "websocket", "path": "/ws/x", "headers": []}, receive, send)
    await ws.accept()
    return ws, sent


async def _close_waits_socket(started: asyncio.Event, release: asyncio.Event) -> WebSocket:
    """close() 停在 ASGI send 上，让调用方在这次让出里换上新连接。"""

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        if message["type"] == "websocket.close":
            started.set()
            await release.wait()

    ws = WebSocket({"type": "websocket", "path": "/ws/x", "headers": []}, receive, send)
    await ws.accept()
    return ws


async def _send_raises_socket() -> WebSocket:
    """复刻发送竞态：状态检查通过，但真正 send 时连接已被关掉。"""

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
    """gss 是全局单例，测试必须还原 active_ws / active_bot 以免互相污染。"""
    saved_ws = dict(gss.active_ws)
    saved_bot = dict(gss.active_bot)
    yield
    gss.active_ws.clear()
    gss.active_ws.update(saved_ws)
    gss.active_bot.clear()
    gss.active_bot.update(saved_bot)


def test_gs_logger_delivers_when_connected() -> None:
    """正向：连接正常时日志必须真的发出去（防止把守卫写成永不发送）。"""

    async def scenario() -> None:
        ws, sent = await _connected_socket()
        await GsLogger(BOT_ID, ws).info("hello")
        assert [m["type"] for m in sent] == ["websocket.accept", "websocket.send"]

    asyncio.run(scenario())


def test_gs_logger_silent_after_close() -> None:
    """断连后推日志不得抛异常 —— 这正是 issue #284 刷屏的那一行。"""

    async def scenario() -> None:
        ws, sent = await _connected_socket()
        glog = GsLogger(BOT_ID, ws)
        await ws.close(code=1001)
        await glog.info("after close")
        await glog.warning("after close")
        await glog.error("after close")
        await glog.success("after close")
        assert ws.application_state == WebSocketState.DISCONNECTED
        assert not any(m["type"] == "websocket.send" for m in sent)

    asyncio.run(scenario())


def test_gs_logger_swallows_send_race() -> None:
    """状态检查与真正 send 之间断连：异常要被吞掉并丢掉 socket 引用。"""

    async def scenario() -> None:
        ws = await _send_raises_socket()
        glog = GsLogger(BOT_ID, ws)
        await glog.info("raced")
        assert glog.bot is None
        # 引用已丢，后续调用直接短路，不会再构造异常
        await glog.info("raced again")

    asyncio.run(scenario())


def test_stale_disconnect_leaves_new_connection_alone() -> None:
    """重连后旧连接的收尾不得拆掉新连接（issue #284 的核心回归）。"""

    async def scenario() -> None:
        stale_ws, _ = await _connected_socket()
        fresh_ws, fresh_sent = await _connected_socket()
        bot = _Bot(BOT_ID, fresh_ws)
        gss.active_ws[BOT_ID] = fresh_ws
        gss.active_bot[BOT_ID] = bot

        # 旧连接的 finally 此时才跑，而 active_ws 里登记的已经是新连接
        await gss.disconnect(BOT_ID, stale_ws)

        assert gss.active_ws.get(BOT_ID) is fresh_ws
        assert bot.bot is fresh_ws
        assert bot._disconnected_at is None
        assert fresh_ws.application_state == WebSocketState.CONNECTED
        assert not any(m["type"] == "websocket.close" for m in fresh_sent)

    asyncio.run(scenario())


def test_current_disconnect_tears_down() -> None:
    """当前连接自己收尾时仍要正常拆干净（正向回归）。"""

    async def scenario() -> None:
        ws, sent = await _connected_socket()
        bot = _Bot(BOT_ID, ws)
        gss.active_ws[BOT_ID] = ws
        gss.active_bot[BOT_ID] = bot

        await gss.disconnect(BOT_ID, ws)

        assert BOT_ID not in gss.active_ws
        assert bot.bot is None
        assert bot._disconnected_at is not None
        assert any(m["type"] == "websocket.close" for m in sent)

    asyncio.run(scenario())


def test_disconnect_repeat_is_idempotent() -> None:
    """core.py 会对同一条连接收尾两次，第二次必须空转而非误伤。"""

    async def scenario() -> None:
        ws, sent = await _connected_socket()
        bot = _Bot(BOT_ID, ws)
        gss.active_ws[BOT_ID] = ws
        gss.active_bot[BOT_ID] = bot

        await gss.disconnect(BOT_ID, ws)
        closed_after_first = sum(1 for m in sent if m["type"] == "websocket.close")
        await gss.disconnect(BOT_ID, ws)

        assert sum(1 for m in sent if m["type"] == "websocket.close") == closed_after_first == 1
        assert bot._disconnected_at is not None

    asyncio.run(scenario())


def test_disconnect_without_websocket_keeps_legacy_behavior() -> None:
    """不传 websocket 的旧调用方式（插件可能这么用）语义不变。"""

    async def scenario() -> None:
        ws, sent = await _connected_socket()
        gss.active_ws[BOT_ID] = ws
        gss.active_bot[BOT_ID] = _Bot(BOT_ID, ws)

        await gss.disconnect(BOT_ID)

        assert BOT_ID not in gss.active_ws
        assert any(m["type"] == "websocket.close" for m in sent)

    asyncio.run(scenario())


def test_gs_logger_swallows_websocket_disconnected() -> None:
    """应用状态仍是 CONNECTED 时，WebSocketDisconnected 不得逃出。"""

    async def scenario() -> None:
        async def receive() -> Message:
            return {"type": "websocket.connect"}

        async def send(message: Message) -> None:
            if message["type"] == "websocket.send":
                raise WebSocketDisconnected("disconnected")

        ws = WebSocket({"type": "websocket", "path": "/ws/x", "headers": []}, receive, send)
        await ws.accept()
        glog = GsLogger(BOT_ID, ws)
        await glog.info("gone")
        assert glog.bot is None

    asyncio.run(scenario())


def test_gs_logger_reraises_unrelated_runtime_error() -> None:
    """不是断连的 RuntimeError 应继续抛出，套接字引用留下。"""

    async def scenario() -> None:
        async def receive() -> Message:
            return {"type": "websocket.connect"}

        async def send(message: Message) -> None:
            if message["type"] == "websocket.send":
                raise RuntimeError("Expected ASGI message websocket.receive")

        ws = WebSocket({"type": "websocket", "path": "/ws/x", "headers": []}, receive, send)
        await ws.accept()
        glog = GsLogger(BOT_ID, ws)
        with pytest.raises(RuntimeError, match="websocket.receive"):
            await glog.info("nope")
        assert glog.bot is ws

    asyncio.run(scenario())


def test_disconnect_during_close_keeps_replacement() -> None:
    """close() 让出时登记的新连接必须留下，发送任务也不得被取消。"""

    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        stale_ws = await _close_waits_socket(started, release)
        fresh_ws, fresh_sent = await _connected_socket()
        gss.active_ws[BOT_ID] = stale_ws
        gss.active_bot[BOT_ID] = _Bot(BOT_ID, stale_ws)
        new_bot = _Bot(BOT_ID, fresh_ws)

        async def hold() -> None:
            await asyncio.Event().wait()

        send_task = asyncio.create_task(hold())
        new_bot._send_task = send_task

        async def replace_during_close() -> None:
            await started.wait()
            gss.active_ws[BOT_ID] = fresh_ws
            gss.active_bot[BOT_ID] = new_bot
            release.set()

        replacer = asyncio.create_task(replace_during_close())
        try:
            await gss.disconnect(BOT_ID, stale_ws)
            await replacer
            assert gss.active_ws.get(BOT_ID) is fresh_ws
            assert gss.active_bot.get(BOT_ID) is new_bot
            assert new_bot.bot is fresh_ws
            assert new_bot._disconnected_at is None
            assert send_task.cancelled() is False
            assert not any(message["type"] == "websocket.close" for message in fresh_sent)
        finally:
            send_task.cancel()
            try:
                await send_task
            except asyncio.CancelledError:
                pass

    asyncio.run(asyncio.wait_for(scenario(), timeout=2))


def test_disconnect_finishes_when_map_entry_is_gone() -> None:
    """active_ws 已删除且 _Bot 还没标断连时，下一次收尾应把清理做完。"""

    async def scenario() -> None:
        ws, sent = await _connected_socket()
        bot = _Bot(BOT_ID, ws)
        gss.active_bot[BOT_ID] = bot

        await gss.disconnect(BOT_ID, ws)

        assert bot.bot is None
        assert bot._disconnected_at is not None
        assert BOT_ID not in gss.active_ws
        assert not any(message["type"] == "websocket.close" for message in sent)

    asyncio.run(scenario())


def test_bot_logger_tracks_reconnect() -> None:
    """Bot.logger 必须动态取，不能快照到重连前的旧 GsLogger。"""
    low = _Bot("ws-alpha")
    ev = Event(bot_id="onebot", user_type="direct", user_id="u1", WS_BOT_ID="ws-alpha")
    high = Bot(low, ev)

    first = high.logger
    replacement = GsLogger("ws-alpha", None)
    low.logger = replacement

    assert high.logger is replacement
    assert high.logger is not first
