import sys
import asyncio
import argparse
from typing import Dict
from asyncio import CancelledError
from pathlib import Path

import uvicorn
from fastapi import WebSocket, WebSocketDisconnect
from msgspec import json as msgjson, to_builtins

from gsuid_core.version import __version__

sys.path.append(str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[1]))

# from gsuid_core.utils.database.startup import exec_list  # noqa: E402

ASCII_FONT = f"""
.------..------..------..------..------..------..------.
|G.--. ||S.--. ||-.--. ||C.--. ||O.--. ||R.--. ||E.--. |
| :/\: || :/\: || (\/) || :/\: || :/\: || :(): || (\/) |
| :\/: || :\/: || :\/: || :\/: || :\/: || ()() || :\/: |
| '--'G|| '--'S|| '--'-|| '--'C|| '--'O|| '--'R|| '--'E|
`------'`------'`------'`------'`------'`------'`------'

          🌱 [早柚核心] 已启动! 版本 {__version__} ！
"""  # noqa: W605


async def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--dev",
        default=False,
        action="store_true",
        help="启用开发模式",
    )
    parser.add_argument(
        "--port",
        default=None,
        type=str,
        help="监听端口（默认: 8765）",
    )
    parser.add_argument(
        "--host",
        default=None,
        type=str,
        help="监听地址 (0.0.0.0 = 监听全部地址，默认: localhost = 只允许本地访问)",
    )
    args = parser.parse_args()

    import time

    start_time = time.time()
    from gsuid_core.utils.database.base_models import init_database

    await init_database()

    from gsuid_core.gss import gss, load_gss  # noqa: E402

    await load_gss(args.dev)

    from gsuid_core.bot import _Bot
    from gsuid_core.config import core_config
    from gsuid_core.logger import logger
    from gsuid_core.models import MessageReceive
    from gsuid_core.handler import handle_event
    from gsuid_core.utils.database.startup import (  # noqa: F401
        trans_adapter as ta,
    )

    if args.port:
        core_config.set_config("PORT", args.port)
    if args.host:
        core_config.set_config("HOST", args.host)

    HOST = core_config.get_config("HOST").lower()
    PORT = int(core_config.get_config("PORT"))
    ENABLE_HTTP = core_config.get_config("ENABLE_HTTP")

    if HOST == "all" or HOST == "none" or HOST == "dual" or not HOST:
        HOST = None

    if args.dev:
        from .app_life import app
    else:
        from gsuid_core.web_app import app, site

    @app.websocket("/ws/{bot_id}")
    async def websocket_endpoint(websocket: WebSocket, bot_id: str):
        try:
            bot = await gss.connect(websocket, bot_id)

            async def start():
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        msg = msgjson.decode(data, type=MessageReceive)
                        await handle_event(bot, msg)
                except WebSocketDisconnect:
                    await gss.disconnect(bot_id)

            async def process():
                await bot._process()

            logger.info("[GsCore] 启动WS服务中...")
            await asyncio.gather(process(), start())
        except CancelledError:
            await gss.disconnect(bot_id)
        finally:
            await gss.disconnect(bot_id)

    if ENABLE_HTTP:
        _bot = _Bot("HTTP")

        @app.post("/api/send_msg")
        async def sendMsg(msg: Dict):
            data = msgjson.encode(msg)
            MR = msgjson.Decoder(MessageReceive).decode(data)
            result = await handle_event(_bot, MR, True)
            if result:
                return {"status_code": 200, "data": to_builtins(result)}
            else:
                return {"status_code": -100, "data": None}

    if not args.dev:
        site.gen_plugin_page()
        site.mount_app(app)

    config = uvicorn.Config(
        app,
        host=HOST,  # type: ignore
        port=PORT,
        log_config=None,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    end_time = time.time()
    logger.success(ASCII_FONT)
    duration = round(end_time - start_time, 2)
    logger.success(f"🚀 [GsCore] 启动完成, 耗时: {duration:.2f}s, 版本: {__version__}")
    await server.serve()


asyncio.run(main())
