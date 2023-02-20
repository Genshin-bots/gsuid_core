import asyncio

import uvicorn
from gss import gss
from config import core_config
from handler import handle_event
from models import MessageReceive
from msgspec import json as msgjson
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

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


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
