import sys
import importlib
from typing import Dict
from pathlib import Path

from fastapi import WebSocket


class Bot:
    def __init__(self, _id: str):
        self.bot_id = _id
        self.bot = gss.active_bot[_id]

    async def send(self, message):
        await self.bot.send_text(message)


class GsServer:
    def __init__(self):
        self.active_bot: Dict[str, WebSocket] = {}
        self.load_plugins()

    def load_plugins(self):
        sys.path.append(str(Path(__file__).parents[1]))
        plug_path = Path(__file__).parent / 'plugins'
        for plugin in plug_path.iterdir():
            if plugin.suffix == '.py':
                importlib.import_module(f'plugins.{plugin.name[:-3]}')
                print(f'插件【{plugin.name}】加载成功！')

    async def connect(self, websocket: WebSocket, bot_id: str) -> Bot:
        await websocket.accept()
        self.active_bot[bot_id] = websocket
        print(f'{bot_id}已连接！')
        return Bot(bot_id)

    def disconnect(self, bot_id: str):
        del self.active_bot[bot_id]
        print(f'{bot_id}已中断！')

    async def send(self, message: str, bot_id: str):
        await self.active_bot[bot_id].send_text(message)

    async def broadcast(self, message: str):
        for bot_id in self.active_bot:
            await self.send(message, bot_id)


gss = GsServer()
