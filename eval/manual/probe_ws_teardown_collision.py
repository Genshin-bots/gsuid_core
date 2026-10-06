"""探针 4：证明 issue #284 背后真正可复现的缺陷 —— gss.disconnect 只按 bot_id 认人。

旧连接的收尾会把「重连后新建的那条连接」一起拆掉。
对照组 = 先关旧再连新；实验组 = 先连新再关旧（适配器重连的真实顺序）。
"""

import sys
import asyncio
from pathlib import Path

import _ws_env
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from msgspec import json as msgjson

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gsuid_core.gss import gss  # noqa: E402
from gsuid_core.models import MessageReceive  # noqa: E402

PORT = 8797


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
        finally:
            await gss.disconnect(bot_id)


async def alive(conn) -> bool:
    """连接是否还活着：能 ping 通就算活。"""
    try:
        pong = await asyncio.wait_for(conn.ping(), timeout=2.0)
        await asyncio.wait_for(pong, timeout=2.0)
        return True
    except Exception:
        return False


async def scenario(connect, bot_id: str, order: str) -> dict[str, object]:
    gss.active_ws.clear()
    gss.active_bot.clear()
    url = f"ws://127.0.0.1:{PORT}/ws/{bot_id}?token=x"

    if order == "old_first_then_new":  # 对照：旧连接先断，新连接后连
        old = await connect(url)
        await old.send(b"{}")
        await asyncio.sleep(0.3)
        await old.close()
        await asyncio.sleep(0.5)
        new = await connect(url)
        await new.send(b"{}")
        await asyncio.sleep(0.6)
    else:  # 实验：新连接先建好，旧连接随后才断（适配器重连的真实顺序）
        old = await connect(url)
        await old.send(b"{}")
        await asyncio.sleep(0.3)
        new = await connect(url)
        await new.send(b"{}")
        await asyncio.sleep(0.3)
        await old.close()
        await asyncio.sleep(0.8)

    new_alive = await alive(new)
    result = {
        "order": order,
        "new_conn_alive": new_alive,
        "active_ws_has_bot": bot_id in gss.active_ws,
        "bot_ws_bound": getattr(gss.active_bot.get(bot_id), "bot", None) is not None,
    }
    try:
        await new.close()
    except Exception:
        pass
    await asyncio.sleep(0.2)
    gss.active_ws.clear()
    gss.active_bot.clear()
    return result


async def main() -> None:
    from websockets.asyncio.client import connect

    app = FastAPI()
    await make_endpoint(app)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="critical")
    server, serve_task = await _ws_env.start_uvicorn(config)

    control = await scenario(connect, "ctl-bot", "old_first_then_new")
    test = await scenario(connect, "exp-bot", "new_first_then_old_closes")

    print("\n" + "=" * 72)
    print(f"{'对照：先关旧连接，再连新连接':<34} {control}")
    print(f"{'实验：先连新连接，旧连接随后断开':<34} {test}")
    print("=" * 72)

    ok = control["new_conn_alive"] and test["new_conn_alive"]
    print(f"对照组新连接存活 : {control['new_conn_alive']}  (期望 True)")
    print(f"实验组新连接存活 : {test['new_conn_alive']}  (期望 True)")
    print(f"实验组 active_ws 仍登记 : {test['active_ws_has_bot']}  (期望 True)")
    print(f"实验组 bot 仍绑定 ws   : {test['bot_ws_bound']}  (期望 True)")
    print()
    print("VERDICT:", "PASS（缺陷不存在）" if ok else "FAIL —— 旧连接的收尾拆掉了新的连接")

    server.should_exit = True
    await serve_task


if __name__ == "__main__":
    asyncio.run(main())
