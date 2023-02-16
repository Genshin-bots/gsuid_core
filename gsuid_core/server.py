import sys
import asyncio
import importlib
from pathlib import Path
from typing import Dict, List, Union, Literal, Optional

from fastapi import WebSocket
from segment import MessageSegment
from msgspec import json as msgjson
from models import Message, MessageSend

sys.path.append(str(Path(__file__).parents[1]))


class Bot:
    def __init__(self, _id: str):
        self.bot_id = _id
        self.bot = gss.active_bot[_id]
        self.logger = GsLogger(self.bot_id)
        self.queue = asyncio.queues.Queue()
        self.background_tasks = set()
        self.user_id: Optional[str] = None
        self.group_id: Optional[str] = None
        self.user_type: Optional[str] = None

    async def send(self, message: Union[Message, List[Message], str]):
        if isinstance(message, Message):
            message = [message]
        elif isinstance(message, str):
            if message.startswith('base64://'):
                message = [MessageSegment.image(message)]
            else:
                message = [MessageSegment.text(message)]
        send = MessageSend(
            content=message,
            bot_id=self.bot_id,
            target_type=self.user_type,
            target_id=self.group_id if self.group_id else self.user_id,
        )
        print(f'[发送消息] {send}')
        await self.bot.send_bytes(msgjson.encode(send))

    async def _process(self):
        while True:
            data = await self.queue.get()
            task = asyncio.create_task(data)
            self.background_tasks.add(task)
            task.add_done_callback(
                lambda _: self.background_tasks.discard(task)
            )


class GsLogger:
    def __init__(self, bot_id: str):
        self.bot_id = bot_id
        self.bot = gss.active_bot[bot_id]

    def get_msg_send(
        self, type: Literal['INFO', 'WARNING', 'ERROR', 'SUCCESS'], msg: str
    ):
        return MessageSend(
            content=[MessageSegment.log(type, msg)],
            bot_id=self.bot_id,
            target_type=None,
            target_id=None,
        )

    async def info(self, msg: str):
        await self.bot.send_bytes(
            msgjson.encode(self.get_msg_send('INFO', msg))
        )

    async def warning(self, msg: str):
        await self.bot.send_bytes(
            msgjson.encode(self.get_msg_send('WARNING', msg))
        )

    async def error(self, msg: str):
        await self.bot.send_bytes(
            msgjson.encode(self.get_msg_send('ERROR', msg))
        )

    async def success(self, msg: str):
        await self.bot.send_bytes(
            msgjson.encode(self.get_msg_send('SUCCESS', msg))
        )


class GsServer:
    def __init__(self):
        self.active_bot: Dict[str, WebSocket] = {}
        self.load_plugins()

    def load_plugins(self):
        sys.path.append(str(Path(__file__).parents[1]))
        plug_path = Path(__file__).parent / 'plugins'
        # 遍历插件文件夹内所有文件
        for plugin in plug_path.iterdir():
            # 如果发现文件夹，则视为插件包
            if plugin.is_dir():
                plugin_path = plugin / '__init__.py'
                plugins_path = plugin / '__full__.py'
                # 如果文件夹内有__full_.py，则视为插件包合集
                sys.path.append(str(plugin_path.parents))
                if plugins_path.exists():
                    importlib.import_module(f'plugins.{plugin.name}.__full__')
                    for sub_plugin in plugin.iterdir():
                        if sub_plugin.is_dir():
                            plugin_path = sub_plugin / '__init__.py'
                            if plugin_path.exists():
                                sys.path.append(str(plugin_path.parents))
                                _p = f'plugins.{plugin.name}.{sub_plugin.name}'
                                importlib.import_module(f'{_p}.__init__')
                # 如果文件夹内有__init_.py，则视为单个插件包
                elif plugin_path.exists():
                    importlib.import_module(f'plugins.{plugin.name}.__init__')
            # 如果发现单文件，则视为单文件插件
            if plugin.suffix == '.py':
                importlib.import_module(f'plugins.{plugin.name[:-3]}')

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
