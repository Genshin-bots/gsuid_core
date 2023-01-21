import sys
import json
import asyncio
import importlib
from pathlib import Path
from typing import Set, Union

from trigger import TL
import websockets.client
import websockets.server
from model import MessageReceive
from pydantic import parse_obj_as


class GsServer:
    def __init__(self, IP: str = 'localhost', PORT: Union[str, int] = '8765'):
        print('核心启动中...')
        self.ws = f'ws://{IP}:{PORT}'
        print(f'WS服务器地址为:{self.ws},等待客户端连接中...')
        self.load_plugins()
        self.clients: Set[websockets.server.WebSocketServerProtocol] = set()

    def load_plugins(self):
        sys.path.append(str(Path(__file__).parents[1]))
        plug_path = Path(__file__).parent / 'plugins'
        for plugin in plug_path.iterdir():
            if plugin.suffix == '.py':
                print(f'插件【{plugin.name}】加载成功！')
                importlib.import_module(f'plugins.{plugin.name[:-3]}')

    async def register(self, ws: websockets.server.WebSocketServerProtocol):
        self.clients.add(ws)
        print(f'{ws.remote_address}已连接！')

    async def unregister(self, ws: websockets.server.WebSocketServerProtocol):
        self.clients.remove(ws)
        print(f'{ws.remote_address}已断开！')

    async def recv_msg(self, ws: websockets.server.WebSocketServerProtocol):
        async for message in ws:
            msg: MessageReceive = parse_obj_as(
                MessageReceive, json.loads(message)
            )
            print(msg)
            for trigger in TL.lst:
                if trigger.check_command(msg):
                    await trigger.func(ws, msg)
                    break
            else:
                await ws.send('已收到消息...')

    async def send_msg(self, ws: websockets.server.WebSocketServerProtocol):
        while True:
            await ws.send('122')

    async def handler(self, ws: websockets.server.WebSocketServerProtocol):
        await self.register(ws)
        try:
            # send_task = asyncio.create_task(self.send_msg(ws))
            recv_task = asyncio.create_task(self.recv_msg(ws))
            await asyncio.gather(recv_task)
        finally:
            await self.unregister(ws)

    async def start(self):
        async with websockets.server.serve(
            self.handler, "localhost", 8766, ping_interval=None
        ):
            await asyncio.Future()


gss = GsServer()
asyncio.run(gss.start())
