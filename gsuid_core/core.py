import sys
import signal
import asyncio
import argparse
from typing import Dict
from asyncio import CancelledError
from pathlib import Path
from dataclasses import dataclass

import uvicorn
from fastapi import WebSocket, WebSocketDisconnect
from msgspec import json as msgjson, to_builtins

from gsuid_core.version import __version__
from gsuid_core.shutdown import shutdown_event

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


@dataclass
class IPStatus:
    failed_count: int = 0
    ban_until: float = 0


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

    import gsuid_core.ai_core.buildin_tools  # noqa: F401
    from gsuid_core.bot import _Bot
    from gsuid_core.config import core_config
    from gsuid_core.logger import logger
    from gsuid_core.models import MessageReceive
    from gsuid_core.handler import handle_event
    from gsuid_core.security_manager import sec_manager
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
    WS_SECRET_TOKEN = core_config.get_config("WS_TOKEN") or ""

    if HOST == "all" or HOST == "none" or HOST == "dual" or not HOST:
        HOST = None

    from .app_life import app

    @app.websocket("/ws/{bot_id}")
    async def websocket_endpoint(websocket: WebSocket, bot_id: str):
        if not websocket.client:
            return

        client_host = websocket.client.host
        token = websocket.query_params.get("token")

        if sec_manager.is_banned(client_host):
            logger.warning(f"🔒️ [GsCore] 拒绝来自已封禁 IP 的连接: {client_host}")
            await websocket.close(code=1008)  # Policy Violation
            return

        if not sec_manager.is_trusted(client_host):
            if not WS_SECRET_TOKEN:
                logger.warning("🔒️ [GsCore] 未配置WS_TOKEN，所有外网连接将被拒绝！")
                await websocket.close(code=1008)
                return

            if token != WS_SECRET_TOKEN:
                sec_manager.record_failure(client_host)
                logger.warning(f"🚨 [GsCore] 非法访问拒绝: IP={client_host}, BotID={bot_id}")
                count = sec_manager.status[client_host].failed_count
                logger.warning(f"🚨 [GsCore] Token 错误!剩余尝试次数: {5 - count}")
                await websocket.close(code=1008)
                return
            else:
                sec_manager.record_success(client_host)

        try:
            bot = await gss.connect(websocket, bot_id)

            async def start():
                try:
                    while not shutdown_event.is_set():
                        try:
                            # 使用 wait_for 添加超时，以便定期检查 shutdown_event
                            data = await asyncio.wait_for(websocket.receive_bytes(), timeout=1.0)
                            msg = msgjson.decode(data, type=MessageReceive)
                            await handle_event(bot, msg)
                        except asyncio.TimeoutError:
                            continue
                        except WebSocketDisconnect:
                            break
                except CancelledError:
                    pass
                finally:
                    await gss.disconnect(bot_id)

            async def process():
                """process 函数的职责是启动 bot._process，由 _process 内部处理 shutdown"""
                try:
                    await bot._process(shutdown_event)
                except CancelledError:
                    pass

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

    config = uvicorn.Config(
        app,
        host=HOST,  # type: ignore
        port=PORT,
        log_config=None,
        loop="asyncio",
    )

    # 设置信号处理，在收到 SIGINT/SIGTERM 时设置 shutdown_event
    # 注意：uvicorn 也会处理这些信号，这里主要是为了通知所有任务
    loop = asyncio.get_event_loop()

    def set_shutdown_event():
        logger.info("[GsCore] 收到关闭信号，正在设置 shutdown_event...")
        shutdown_event.set()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, set_shutdown_event)
    except NotImplementedError:
        # Windows 不支持 add_signal_handler，仅依赖 uvicorn 的信号处理
        logger.debug("[GsCore] 当前平台不支持 add_signal_handler，将依赖 uvicorn 的关闭流程")

    server = uvicorn.Server(config)
    end_time = time.time()
    logger.success(ASCII_FONT)
    duration = round(end_time - start_time, 2)
    logger.success(f"🚀 [GsCore] 启动完成, 耗时: {duration:.2f}s, 版本: {__version__}")

    await server.serve()


asyncio.run(main())
