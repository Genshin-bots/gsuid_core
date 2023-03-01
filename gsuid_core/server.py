import sys
import asyncio
import importlib
from pathlib import Path
from typing import Dict, Callable

from fastapi import WebSocket

from gsuid_core.bot import _Bot
from gsuid_core.logger import logger


class GsServer:
    _instance = None
    is_initialized = False
    is_load = False
    bot_connect_def = set()

    def __new__(cls, *args, **kwargs):
        # 判断sv是否已经被初始化
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not self.is_initialized:
            self.active_ws: Dict[str, WebSocket] = {}
            self.active_bot: Dict[str, _Bot] = {}
            self.is_initialized = True

    def load_plugins(self):
        logger.info('开始导入插件...')
        sys.path.append(str(Path(__file__).parents[1]))
        plug_path = Path(__file__).parent / 'plugins'
        # 遍历插件文件夹内所有文件
        for plugin in plug_path.iterdir():
            # 如果发现文件夹，则视为插件包
            try:
                if plugin.is_dir():
                    plugin_path = plugin / '__init__.py'
                    plugins_path = plugin / '__full__.py'
                    nest_path = plugin / '__nest__.py'
                    # 如果文件夹内有__full_.py，则视为插件包合集
                    sys.path.append(str(plugin_path.parents))
                    if plugins_path.exists():
                        self.load_dir_plugins(plugin)
                    elif nest_path.exists():
                        path = nest_path.parent / plugin.name
                        if path.exists():
                            self.load_dir_plugins(path, True)
                    # 如果文件夹内有__init_.py，则视为单个插件包
                    elif plugin_path.exists():
                        importlib.import_module(
                            f'plugins.{plugin.name}.__init__'
                        )
                # 如果发现单文件，则视为单文件插件
                if plugin.suffix == '.py':
                    importlib.import_module(f'plugins.{plugin.name[:-3]}')
            except Exception as e:  # noqa
                logger.exception(e)
                logger.warning(f'插件{plugin.name}加载失败')

    def load_dir_plugins(self, plugin: Path, nest: bool = False):
        for sub_plugin in plugin.iterdir():
            if sub_plugin.is_dir():
                plugin_path = sub_plugin / '__init__.py'
                if plugin_path.exists():
                    sys.path.append(str(plugin_path.parents))
                    name = plugin.name
                    if nest:
                        _p = f'plugins.{name}.{name}.{sub_plugin.name}'
                    else:
                        _p = f'plugins.{name}.{sub_plugin.name}'
                    importlib.import_module(f'{_p}.__init__')

    async def connect(self, websocket: WebSocket, bot_id: str) -> _Bot:
        await websocket.accept()
        self.active_ws[bot_id] = websocket
        self.active_bot[bot_id] = bot = _Bot(bot_id, websocket)
        logger.info(f'{bot_id}已连接！')
        _task = [_def() for _def in self.bot_connect_def]
        asyncio.gather(*_task)
        return bot

    def disconnect(self, bot_id: str):
        del self.active_ws[bot_id]
        del self.active_bot[bot_id]
        logger.warning(f'{bot_id}已中断！')

    async def send(self, message: str, bot_id: str):
        await self.active_ws[bot_id].send_text(message)

    async def broadcast(self, message: str):
        for bot_id in self.active_ws:
            await self.send(message, bot_id)

    @classmethod
    def on_bot_connect(cls, func: Callable):
        cls.bot_connect_def.add(func)
        return func
