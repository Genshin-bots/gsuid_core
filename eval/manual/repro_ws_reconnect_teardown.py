"""复现 issue #284：断开 WS 时未捕获的 WebSocketDisconnected 刷整屏堆栈。

用真实 GsServer + core.py 的 websocket_endpoint 原样循环 + 真实 uvicorn + 真实 WS 客户端，
验证「重连时旧连接的收尾把新连接拆掉」是否成立，以及是否有未捕获异常逃出 endpoint。

用法：uv run python eval/manual/repro_ws_reconnect_teardown.py
"""

import sys
import asyncio
import traceback
from pathlib import Path

import _ws_env
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from msgspec import json as msgjson

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


from gsuid_core.gss import gss  # noqa: E402
from gsuid_core.models import MessageReceive  # noqa: E402

BOT_ID = "probe-bot"
PORT = 8791

# 逃出 websocket_endpoint 的异常（异常逃出后 uvicorn 才会打整屏堆栈）
ESCAPES: list[tuple[str, str]] = []


async def run_inbound_event(bot, msg, slots) -> None:
    """core.py 里 run_inbound_event 的替身：消息处理与本探针无关。"""
    await asyncio.sleep(0)


async def make_endpoint(app: FastAPI) -> None:
    """core.py:117-208 的原样拷贝（去掉 AI/DB 依赖）。"""

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
                    await gss.disconnect(bot_id)

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
        except Exception as exc:  # 记录逃出 endpoint 的异常，再原样抛出给 uvicorn
            ESCAPES.append((type(exc).__name__, f"{exc}"))
            raise
        finally:
            await gss.disconnect(bot_id)


def snapshot() -> dict[str, object]:
    ws = gss.active_ws.get(BOT_ID)
    bot = gss.active_bot.get(BOT_ID)
    return {
        "active_ws_has_bot": ws is not None,
        "ws_app_state": getattr(ws, "application_state", None),
        "ws_client_state": getattr(ws, "client_state", None),
        "bot_ws_is_none": getattr(bot, "bot", None) is None,
        "bot_disconnected_at": getattr(bot, "_disconnected_at", None),
    }


async def scenario(name: str, body) -> dict[str, object]:
    ESCAPES.clear()
    print(f"\n{'=' * 70}\n{name}\n{'=' * 70}")
    before = snapshot()
    result = await body()
    await asyncio.sleep(1.5)  # 给逃逸异常的传播和 uvicorn 日志留时间
    print(f"  before      : {before}")
    print(f"  after       : {snapshot()}")
    print(f"  result      : {result}")
    print(f"  ESCAPES     : {ESCAPES if ESCAPES else 'none'}")
    return {"name": name, "escapes": list(ESCAPES), "after": snapshot()}


async def main() -> None:
    from websockets.asyncio.client import connect

    app = FastAPI()
    await make_endpoint(app)

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server, serve_task = await _ws_env.start_uvicorn(config)

    url = f"ws://127.0.0.1:{PORT}/ws/{BOT_ID}?token=x"
    report: list[dict[str, object]] = []

    # S1 对照组：单连接，正常关闭 —— 期望干净，无异常逃出
    async def s1():
        async with connect(url) as ws:
            await ws.send(b"{}")
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.3)
        return "single connect, clean close"

    report.append(await scenario("S1 对照：单连接正常断开", s1))
    gss.active_ws.clear()
    gss.active_bot.clear()

    # S2 关键场景：新连接先建立，旧连接再断开（旧连接的收尾按 bot_id 拆新连接）
    async def s2():
        old = await connect(url)
        await old.send(b"{}")
        await asyncio.sleep(0.3)
        new = await connect(url)  # 重连：同一 bot_id，active_ws[bot_id] 变成 new
        await new.send(b"{}")
        await asyncio.sleep(0.3)
        print(f"  [reconnect done] active_ws==new? {gss.active_ws.get(BOT_ID) is new}")
        await old.close()  # 旧连接这时才断，触发它的 finally -> gss.disconnect(bot_id)
        await asyncio.sleep(1.0)
        try:
            await asyncio.wait_for(new.recv(), timeout=1.5)
            new_closed_by_server = False
        except Exception as exc:
            new_closed_by_server = f"{type(exc).__name__}"
        print(f"  [new conn] closed_by_server={new_closed_by_server}")
        try:
            await new.close()
        except Exception:
            pass
        return f"new_conn_closed_by_server={new_closed_by_server}"

    report.append(await scenario("S2 重连后旧连接断开（关键场景）", s2))
    gss.active_ws.clear()
    gss.active_bot.clear()

    # S3 适配器抖动：反复「连上 -> 立刻被拆」，共 6 轮
    async def s3():
        for i in range(6):
            a = await connect(url)
            await asyncio.sleep(0.15)
            b = await connect(url)
            await asyncio.sleep(0.15)
            await a.close()
            await asyncio.sleep(0.15)
            try:
                await b.close()
            except Exception:
                pass
            await asyncio.sleep(0.15)
        return f"{6} reconnect flaps"

    report.append(await scenario("S3 适配器重连抖动 x6", s3))
    gss.active_ws.clear()
    gss.active_bot.clear()

    # S4 服务端在已关闭的 socket 上发送 —— starlette send() 的 WebSocketDisconnected 分支
    async def s4():
        async with connect(url) as ws:
            await ws.send(b"{}")
            await asyncio.sleep(0.2)
            socket = gss.active_ws[BOT_ID]
            await socket.close(code=1001)  # application_state -> DISCONNECTED
            print(f"  [closed] app_state={socket.application_state}")
            try:
                await socket.send_bytes(b"{}")
            except Exception as exc:
                print(f"  [send after close] {type(exc).__name__}: {exc}")
                return f"send_after_close={type(exc).__name__}"
            return "send_after_close=no-raise"
        return "done"

    report.append(await scenario("S4 已关闭 socket 上再发送", s4))

    server.should_exit = True
    await serve_task

    print(f"\n{'=' * 70}\nSUMMARY\n{'=' * 70}")
    total = 0
    for item in report:
        escaped = item["escapes"]
        names: list[str] = []
        if isinstance(escaped, list):
            total += len(escaped)
            names = [str(row[0]) for row in escaped if isinstance(row, tuple) and row]
        print(f"{item['name']}: escapes={names if names else 'none'}")
    print(f"\ntotal escaped exceptions: {total}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
