"""探针 5：证明 WebSocketDisconnected 的真实逃逸点在 gs_logger.py:25，而不是 core.py:170。

用真实 GsLogger + 真实 uvicorn + 真实 WS 连接：连接关闭后调用 logger.info()，
看异常是否无人捕获地逃出。
"""

import sys
import asyncio
import traceback
from pathlib import Path

import _ws_env
import uvicorn
from fastapi import FastAPI, WebSocket

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gsuid_core.gs_logger import GsLogger  # noqa: E402

PORT = 8799
ESCAPED: list[str] = []
HELD: list[GsLogger] = []


async def make_endpoint(app: FastAPI) -> None:

    @app.websocket("/ws/{bot_id}")
    async def websocket_endpoint(websocket: WebSocket, bot_id: str):
        await websocket.accept()
        # 模拟 server.py:617 —— 每条连接一个 GsLogger，且 Bot 在构造时快照了它（bot.py:745）
        glog = GsLogger(bot_id, websocket)
        HELD.append(glog)
        try:
            await websocket.receive_bytes()
        except BaseException as exc:
            ESCAPED.append(f"receive: {type(exc).__name__}")
            raise
        finally:
            pass


async def main() -> None:
    from websockets.asyncio.client import connect

    app = FastAPI()
    await make_endpoint(app)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server, serve_task = await _ws_env.start_uvicorn(config)

    url = f"ws://127.0.0.1:{PORT}/ws/x?token=t"
    client = await connect(url)
    await asyncio.sleep(0.4)
    await client.close()  # 断开后服务端 socket 变 DISCONNECTED
    await asyncio.sleep(0.6)

    glog = HELD[0]
    ws = glog.bot
    print(f"socket.application_state = {None if ws is None else ws.application_state}")
    print("\n调用 GsLogger.info()（连接已断开）：")
    try:
        await glog.info("bot log line after disconnect")
        print("  -> 没有抛异常")
    except BaseException as exc:
        print(f"  -> 抛出 {type(exc).__name__}: {exc}")
        print("  -> GsLogger._send 无 try/except，该异常原样上抛")
        print("\n逃逸链（未捕获）：")
        traceback.print_exc()

    server.should_exit = True
    await serve_task


if __name__ == "__main__":
    asyncio.run(main())
