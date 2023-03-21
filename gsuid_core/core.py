import sys
import asyncio
from typing import Dict
from pathlib import Path

import uvicorn
from msgspec import json as msgjson
from starlette.requests import Request
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

sys.path.append(str(Path(__file__).parents[1]))
from gsuid_core.sv import SL  # noqa: E402
from gsuid_core.gss import gss  # noqa: E402
from gsuid_core.logger import logger  # noqa: E402
from gsuid_core.config import core_config  # noqa: E402
from gsuid_core.handler import handle_event  # noqa: E402
from gsuid_core.models import MessageReceive  # noqa: E402
from gsuid_core.aps import start_scheduler, shutdown_scheduler  # noqa: E402

app = FastAPI()
HOST = core_config.get_config('HOST')
PORT = int(core_config.get_config('PORT'))


@app.websocket('/ws/{bot_id}')
async def websocket_endpoint(websocket: WebSocket, bot_id: str):
    bot = await gss.connect(websocket, bot_id)

    async def start():
        try:
            while True:
                data = await websocket.receive_bytes()
                msg = msgjson.decode(data, type=MessageReceive)
                await handle_event(bot, msg)
        except WebSocketDisconnect:
            gss.disconnect(bot_id)

    async def process():
        await bot._process()

    await asyncio.gather(process(), start())


@app.on_event('startup')
async def startup_event():
    try:
        from gsuid_core.webconsole.__init__ import start_check

        await start_check()
    except ImportError:
        logger.warning('未加载GenshinUID...网页控制台启动失败...')
    await start_scheduler()


@app.on_event('shutdown')
async def shutdown_event():
    await shutdown_scheduler()


if __name__ == "__main__":
    try:
        from gsuid_core.webconsole.mount_app import site

        @app.post('/setSV/{name}')
        @site.auth.requires('admin')
        async def _set_SV(request: Request, data: Dict, name: str):
            if name in SL.lst:
                sv = SL.lst[name]
                data['pm'] = int(data['pm'])
                data['black_list'] = data['black_list'].split(';')
                sv.set(**data)

        site.mount_app(app)
    except ImportError:
        logger.warning('未加载GenshinUID...网页控制台启动失败...')

    uvicorn.run(app, host=HOST, port=PORT)
