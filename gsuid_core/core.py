import sys
import asyncio
from typing import Dict
from pathlib import Path
from asyncio import CancelledError

import uvicorn
from msgspec import to_builtins
from msgspec import json as msgjson
from fastapi import WebSocket, WebSocketDisconnect

sys.path.append(str(Path(__file__).resolve().parent))
sys.path.append(str(Path(__file__).resolve().parents[1]))

# from gsuid_core.utils.database.startup import exec_list  # noqa: E402


async def main():
    from gsuid_core.utils.database.base_models import init_database

    await init_database()

    from gsuid_core.gss import gss  # noqa: E402
    from gsuid_core.bot import _Bot  # noqa: E402
    from gsuid_core.logger import logger  # noqa: E402
    from gsuid_core.web_app import app, site  # noqa: E402
    from gsuid_core.config import core_config  # noqa: E402
    from gsuid_core.handler import handle_event  # noqa: E402
    from gsuid_core.models import MessageReceive  # noqa: E402

    HOST = core_config.get_config('HOST')
    PORT = int(core_config.get_config('PORT'))
    ENABLE_HTTP = core_config.get_config('ENABLE_HTTP')

    @app.websocket('/ws/{bot_id}')
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

            logger.info('[GsCore] 启动WS服务中...')
            await asyncio.gather(process(), start())
        except CancelledError:
            await gss.disconnect(bot_id)
        finally:
            await gss.disconnect(bot_id)

    if ENABLE_HTTP:
        _bot = _Bot('HTTP')

        @app.post('/api/send_msg')
        async def sendMsg(msg: Dict):
            data = msgjson.encode(msg)
            MR = msgjson.Decoder(MessageReceive).decode(data)
            result = await handle_event(_bot, MR, True)
            if result:
                return {'status_code': 200, 'data': to_builtins(result)}
            else:
                return {'status_code': -100, 'data': None}

    site.gen_plugin_page()
    site.mount_app(app)

    config = uvicorn.Config(
        app,
        host=HOST,
        port=PORT,
        log_config={
            'version': 1,
            'disable_existing_loggers': False,
            'handlers': {
                'default': {
                    'class': 'gsuid_core.logger.LoguruHandler',
                },
            },
            'loggers': {
                'uvicorn.error': {'handlers': ['default'], 'level': 'INFO'},
                'uvicorn.access': {
                    'handlers': ['default'],
                    'level': 'INFO',
                },
            },
        },  # 你的日志配置
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    await server.serve()


asyncio.run(main())
