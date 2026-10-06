"""探针 2：定位 WebSocketDisconnected 的真实来源。

对 starlette 的 receive_bytes / send_bytes / send 打点，记录每个分支实际抛什么，
并统计有多少个 WebSocketDisconnected 逃出 endpoint。覆盖 5 种断开方式。
"""

import sys
import asyncio
import traceback
from pathlib import Path

import _ws_env
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from msgspec import json as msgjson
from starlette.websockets import WebSocket as StarletteWS, WebSocketDisconnected

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gsuid_core.gss import gss  # noqa: E402
from gsuid_core.models import MessageReceive  # noqa: E402

BOT_ID = "probe2-bot"
PORT = 8793

HITS: list[str] = []
ESCAPES: list[str] = []

_orig_receive_bytes = StarletteWS.receive_bytes
_orig_send_bytes = StarletteWS.send_bytes
_orig_send = StarletteWS.send


async def receive_bytes(self):  # noqa: ANN001, ANN201
    try:
        return await _orig_receive_bytes(self)
    except WebSocketDisconnected as exc:
        HITS.append(f"receive_bytes -> WebSocketDisconnected({exc}) app_state={self.application_state}")
        raise


async def send_bytes(self, data):  # noqa: ANN001, ANN201
    try:
        return await _orig_send_bytes(self, data)
    except WebSocketDisconnected as exc:
        HITS.append(f"send_bytes -> WebSocketDisconnected({exc}) app_state={self.application_state}")
        raise
    except BaseException as exc:
        HITS.append(f"send_bytes -> {type(exc).__name__}({exc}) app_state={self.application_state}")
        raise


async def send(self, message):  # noqa: ANN001, ANN201
    try:
        return await _orig_send(self, message)
    except WebSocketDisconnected as exc:
        HITS.append(f"send[{message.get('type')}] -> WebSocketDisconnected({exc})")
        raise


StarletteWS.receive_bytes = receive_bytes  # type: ignore[method-assign]
StarletteWS.send_bytes = send_bytes  # type: ignore[method-assign]
StarletteWS.send = send  # type: ignore[method-assign]


async def run_inbound_event(bot, msg, slots) -> None:
    await asyncio.sleep(0)


async def make_endpoint(app: FastAPI) -> None:

    @app.websocket("/ws/{bot_id}")
    async def websocket_endpoint(websocket: WebSocket, bot_id: str):
        if not websocket.client:
            return
        try:
            bot = await gss.connect(websocket, bot_id)
            inbound_slots = asyncio.Semaphore(8)

            async def start():
                try:
                    while True:
                        try:
                            data = await asyncio.wait_for(websocket.receive_bytes(), timeout=1.0)
                            msg = msgjson.decode(data, type=MessageReceive)
                            if bot.resolve_recall(msg):
                                continue
                            asyncio.create_task(run_inbound_event(bot, msg, inbound_slots))
                        except asyncio.TimeoutError:
                            continue
                        except WebSocketDisconnect:
                            break
                        except (ConnectionResetError, ConnectionAbortedError):
                            break
                except asyncio.CancelledError:
                    pass
                finally:
                    await gss.disconnect(bot_id, websocket)

            async def process():
                await bot._process(None)

            process_task = asyncio.create_task(process())
            start_task = asyncio.create_task(start())
            try:
                await asyncio.wait({process_task, start_task}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for _t in (process_task, start_task):
                    if not _t.done():
                        _t.cancel()
                _results = await asyncio.gather(process_task, start_task, return_exceptions=True)
            for _r in _results:
                if isinstance(_r, BaseException) and not isinstance(_r, asyncio.CancelledError):
                    raise _r
        except BaseException as exc:
            ESCAPES.append(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            await gss.disconnect(bot_id, websocket)


def reset() -> None:
    HITS.clear()
    ESCAPES.clear()
    gss.active_ws.clear()
    gss.active_bot.clear()


async def report(name: str) -> None:
    await asyncio.sleep(1.2)
    print(f"\n--- {name} ---")
    print(f"  starlette branch hits ({len(HITS)}):")
    for h in HITS:
        print(f"      {h}")
    print(f"  escaped endpoint ({len(ESCAPES)}): {ESCAPES or 'none'}")


async def main() -> None:
    from websockets.asyncio.client import connect

    app = FastAPI()
    await make_endpoint(app)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server, serve_task = await _ws_env.start_uvicorn(config)
    url = f"ws://127.0.0.1:{PORT}/ws/{BOT_ID}?token=x"

    # S5 服务端主动断开（心跳超时 / 封禁 / 主动踢），客户端仍在线
    reset()
    ws = await connect(url)
    await ws.send(b"{}")
    await asyncio.sleep(0.4)
    await gss.disconnect(BOT_ID)
    await report("S5 服务端主动 gss.disconnect（客户端仍在线）")
    try:
        await ws.close()
    except Exception:
        pass

    # S6 RST 断开（SO_LINGER 0），最常见的「拔网线」
    reset()
    raw = await connect(url)
    await raw.send(b"{}")
    await asyncio.sleep(0.4)
    sock = raw.transport.get_extra_info("socket")
    if sock is not None:
        import socket as _s
        import struct

        sock.setsockopt(_s.SOL_SOCKET, _s.SO_LINGER, struct.pack("ii", 1, 0))
    raw.transport.abort()
    await report("S6 RST 硬断开")

    # S7 空闲 3 秒（跨 3 个 wait_for 超时窗口）后正常断开
    reset()
    ws = await connect(url)
    await ws.send(b"{}")
    await asyncio.sleep(3.2)
    await ws.close()
    await report("S7 空闲 3 秒后正常断开")

    # S8 连接期间服务端发送 + 并发 close（send/close 竞争）
    reset()
    ws = await connect(url)
    await ws.send(b"{}")
    await asyncio.sleep(0.3)
    socket = gss.active_ws[BOT_ID]
    bot = gss.active_bot[BOT_ID]
    for i in range(40):
        await bot._send_queue.put(_mk_send(socket))
        if i == 5:
            asyncio.create_task(gss.disconnect(BOT_ID))
        await asyncio.sleep(0.01)
    await report("S8 并发 send 与 close 竞争 x40")

    # S9 纯接收侧：先 close 再让循环继续（直接打 receive_bytes 的入口守卫）
    reset()
    ws = await connect(url)
    await ws.send(b"{}")
    await asyncio.sleep(0.3)
    socket = gss.active_ws[BOT_ID]
    await socket.close(code=1001)
    try:
        await socket.receive_bytes()
    except BaseException as exc:
        HITS.append(f"direct receive_bytes after close -> {type(exc).__name__}: {exc}")
    await report("S9 close 后直接 receive_bytes（入口守卫）")

    server.should_exit = True
    await serve_task
    print(f"\nTOTAL hits={len(HITS)} escapes={len(ESCAPES)}")


def _mk_send(socket):  # noqa: ANN001, ANN202

    async def _do_send() -> None:
        await socket.send_bytes(b"{}")

    return _do_send()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
