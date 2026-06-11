import os
import sys
import signal
import asyncio
import argparse
import multiprocessing
from typing import Dict
from asyncio import CancelledError
from pathlib import Path
from dataclasses import dataclass

import uvicorn
from fastapi import WebSocket, WebSocketDisconnect
from msgspec import json as msgjson, to_builtins

from gsuid_core.version import __version__
from gsuid_core.shutdown import shutdown_event
from gsuid_core.startup_info import core_startup_info

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

    from gsuid_core.logger import logger
    from gsuid_core.ai_core.configs.ai_config import ai_config
    from gsuid_core.utils.database.base_models import init_database

    hf_endpoint: str = ai_config.get_config("hf_endpoint").data

    os.environ["HF_HUB_DOWNLOAD_TIMEOUT"] = "60"
    os.environ["HF_ENDPOINT"] = hf_endpoint

    logger.info(f"🧠 [GsCore] 切换HF地址，地址: {hf_endpoint}")

    await init_database()

    from gsuid_core.gss import gss, load_gss  # noqa: E402

    await load_gss(args.dev)

    # 注册 AI 核心后台初始化钩子（init_ai_core）。该模块本身很轻量，
    # 重依赖（buildin_tools 等）的导入推迟到 WS 启动后的后台阶段执行。
    import gsuid_core.ai_core.startup  # noqa: F401
    from gsuid_core.bot import _Bot
    from gsuid_core.config import core_config
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
                            # 优先拦截 recall_message_id 回执，避免其进入正常消息管道
                            if bot.resolve_recall(msg):
                                continue
                            await handle_event(bot, msg)
                        except asyncio.TimeoutError:
                            continue
                        except WebSocketDisconnect:
                            break
                        except (ConnectionResetError, ConnectionAbortedError):
                            # Windows ProactorEventLoop: 客户端异常断开时抛出
                            # [WinError 995] 由于线程退出或应用程序请求，已中止 I/O 操作
                            logger.debug(f"[GsCore] WebSocket 连接被重置: {bot_id}")
                            break
                except CancelledError:
                    pass
                finally:
                    await gss.disconnect(bot_id)

            async def process():
                try:
                    await bot._process(shutdown_event)
                except CancelledError:
                    pass

            logger.info("[GsCore] 启动WS服务中...")
            # 任一结束(通常是 start 断连返回)即取消另一个, 避免 _process 残留消费同一队列
            process_task = asyncio.create_task(process())
            start_task = asyncio.create_task(start())
            try:
                await asyncio.wait(
                    {process_task, start_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for _t in (process_task, start_task):
                    if not _t.done():
                        _t.cancel()
                # 取回两个子任务结果, 避免 "exception never retrieved" 告警
                _results = await asyncio.gather(process_task, start_task, return_exceptions=True)
            # 子任务有真实异常(非取消)则向上抛; 放在 finally 外, 不吞外层取消
            for _r in _results:
                if isinstance(_r, BaseException) and not isinstance(_r, CancelledError):
                    raise _r
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
    core_startup_info.duration = duration

    from gsuid_core.sv import SL

    trigger_count = sum(len(sv.TL) for sv in SL.lst.values())
    plugin_count = len(SL.plugins)
    sv_count = len(SL.lst)
    logger.success(f"🚀 [GsCore] 启动完成, 耗时: {duration:.2f}s, 版本: {__version__}")
    logger.success(f"📦 插件: {plugin_count} | 🛠️ 服务: {sv_count} | ⚡ 触发器: {trigger_count}")

    # AI 核心统计（仅在 AI 功能启用时显示）
    try:
        if ai_config.get_config("enable").data:
            from gsuid_core.ai_core.register import _TOOL_REGISTRY, get_all_tools
            from gsuid_core.ai_core.persona.resource import list_available_personas
            from gsuid_core.ai_core.configs.provider_config_manager import (
                list_available_provider_configs,
            )

            ai_tool_count = len(get_all_tools())
            trigger_ai_tool_count = len(_TOOL_REGISTRY["by_trigger"]) if "by_trigger" in _TOOL_REGISTRY else 0
            persona_count = len(list_available_personas())
            openai_config_count = len(list_available_provider_configs("openai"))
            anthropic_config_count = len(list_available_provider_configs("anthropic"))
            config_count = openai_config_count + anthropic_config_count

            logger.success(
                f"🧠 AI工具: {ai_tool_count} | 🔗 Trigger工具: {trigger_ai_tool_count} | "
                f"🎭 人格: {persona_count} | 📋 配置文件: {config_count}"
            )
    except Exception as e:
        logger.debug(f"🧠 [GsCore] AI 核心统计输出失败: {e}")

    await server.serve()


# 仅主进程启动服务(spawn 子进程重 import 本模块时跳过)
if multiprocessing.current_process().name == "MainProcess":
    try:
        asyncio.run(main())
    except (ConnectionResetError, ConnectionAbortedError):
        # Windows ProactorEventLoop: 客户端异常断开时可能逃逸到顶层
        pass
    except asyncio.InvalidStateError:
        # Windows ProactorEventLoop 已知问题: 连接重置时 set_exception
        # 可能被调用在已终态的 Future 上，导致 InvalidStateError 逃逸
        pass
