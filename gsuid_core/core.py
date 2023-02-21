import sys
import asyncio
from pathlib import Path

import uvicorn
from msgspec import json as msgjson
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

sys.path.append(str(Path(__file__).parents[1]))
from gsuid_core.gss import gss  # noqa: E402
from gsuid_core.config import core_config  # noqa: E402
from gsuid_core.handler import handle_event  # noqa: E402
from gsuid_core.models import MessageReceive  # noqa: E402
from gsuid_core.aps import start_scheduler, shutdown_scheduler  # noqa: E402

app = FastAPI()
HOST = core_config.get_config('HOST')
PORT = int(core_config.get_config('PORT'))


@app.websocket("/ws/{bot_id}")
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
    await start_scheduler()


@app.on_event('shutdown')
async def shutdown_event():
    await shutdown_scheduler()


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
