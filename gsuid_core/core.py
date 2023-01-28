import json

import uvicorn
from server import gss
from config import core_config
from handler import handle_event
from model import MessageReceive
from pydantic import parse_obj_as
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

app = FastAPI()
HOST = core_config.get_config('HOST')
PORT = int(core_config.get_config('PORT'))


@app.websocket("/ws/{bot_id}")
async def websocket_endpoint(websocket: WebSocket, bot_id: str):
    bot = await gss.connect(websocket, bot_id)

    try:
        while True:
            data = await websocket.receive_text()
            msg = parse_obj_as(MessageReceive, json.loads(data))
            print(msg)
            await handle_event(bot, msg)
    except WebSocketDisconnect:
        gss.disconnect(bot_id)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
