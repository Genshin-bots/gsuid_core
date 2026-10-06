"""探针 3：量化 core.py start() 循环里 receive_bytes 抛 WebSocketDisconnected 的实际概率。

复现两种服务端主动断开（心跳超时/封禁走 gss.disconnect；踢连接走 socket.close），
每种跑 N 轮，统计逃出 endpoint 的未捕获异常 —— 那才是 issue #284 说的「刷整屏堆栈」。
"""

import sys
import asyncio
from pathlib import Path
from collections import Counter

import _ws_env
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from msgspec import json as msgjson

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gsuid_core.gss import gss  # noqa: E402
from gsuid_core.models import MessageReceive  # noqa: E402

BOT_ID = "probe3-bot"
PORT = 8795
TRIALS = 30

ESCAPES: list[str] = []


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
            ESCAPES.append(type(exc).__name__)
            raise
        finally:
            await gss.disconnect(bot_id, websocket)


async def trial(connect, url: str, mode: str) -> None:
    ESCAPES.clear()
    ws = await connect(url)
    await ws.send(b"{}")
    await asyncio.sleep(0.35)
    if mode == "gss_disconnect":
        await gss.disconnect(BOT_ID)
    else:
        socket = gss.active_ws.get(BOT_ID)
        if socket is not None:
            await socket.close(code=1001)
    await asyncio.sleep(0.65)
    try:
        await ws.close()
    except Exception:
        pass
    gss.active_ws.clear()
    gss.active_bot.clear()
    await asyncio.sleep(0.05)


async def main() -> None:
    from websockets.asyncio.client import connect

    app = FastAPI()
    await make_endpoint(app)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="critical")
    server, serve_task = await _ws_env.start_uvicorn(config)
    url = f"ws://127.0.0.1:{PORT}/ws/{BOT_ID}?token=x"

    results: dict[str, Counter[str]] = {}
    for mode in ("gss_disconnect", "socket_close"):
        counter: Counter[str] = Counter()
        for _ in range(TRIALS):
            await trial(connect, url, mode)
            counter.update(ESCAPES)
        results[mode] = counter
        total = sum(counter.values())
        print(f"\n{mode}: {TRIALS} 轮, 逃出 endpoint 的未捕获异常 = {total}")
        for name, n in counter.most_common():
            print(f"    {name}: {n}/{TRIALS} ({n / TRIALS * 100:.0f}%)")
        if total == 0:
            print("    (无)")

    server.should_exit = True
    await serve_task


if __name__ == "__main__":
    asyncio.run(main())
