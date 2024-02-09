import re
import sys
import asyncio
import importlib
import subprocess
from pathlib import Path
from typing import Dict, List, Callable

import toml
import pkg_resources
from fastapi import WebSocket

from gsuid_core.bot import _Bot
from gsuid_core.logger import logger
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config
from gsuid_core.utils.plugins_update._plugins import (
    sync_get_plugin_url,
    sync_change_plugin_url,
)

auto_install_dep: bool = core_plugins_config.get_config('AutoInstallDep').data
start_venv: str = core_plugins_config.get_config('StartVENV').data
core_start_def = set()
core_shutdown_def = set()
installed_dependencies = []
ignore_dep = ['python', 'fastapi', 'pydantic']


def on_core_start(func: Callable):
    if func not in core_start_def:
        core_start_def.add(func)
    return func


def on_core_shutdown(func: Callable):
    if func not in core_shutdown_def:
        core_shutdown_def.add(func)
    return func


def check_start_tool():
    path = Path(__file__).parent.parent
    pdm_python_path = path / '.pdm-python'
    if start_venv == 'auto':
        if pdm_python_path.exists():
            return 'pdm run pip'
        else:
            return 'poetry run pip'
    elif start_venv == 'pdm':
        return 'pdm run pip'
    elif start_venv == 'poetry':
        return 'poetry run pip'
    else:
        return start_venv.strip()


class GsServer:
    _instance = None
    is_initialized = False
    is_load = False
    bot_connect_def = set()

    def __new__(cls, *args, **kwargs):
        # 判断sv是否已经被初始化
        if cls._instance is None:
            cls._instance = super(GsServer, cls).__new__(cls, *args, **kwargs)
        return cls._instance

    def __init__(self):
        if not self.is_initialized:
            self.active_ws: Dict[str, WebSocket] = {}
            self.active_bot: Dict[str, _Bot] = {}
            self.is_initialized = True

    def load_plugins(self):
        logger.info('[GsCore] 开始加载插件...')
        get_installed_dependencies()
        sys.path.append(str(Path(__file__).parents[1]))
        plug_path = Path(__file__).parent / 'plugins'
        # 遍历插件文件夹内所有文件
        for plugin in plug_path.iterdir():
            if plugin.stem.startswith('_'):
                continue
            # 如果发现文件夹，则视为插件包
            logger.trace('===============')
            logger.debug(f'导入{plugin.stem}中...')
            logger.trace('===============')
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
                        pyproject = plugin / 'pyproject.toml'
                        if auto_install_dep and pyproject.exists:
                            check_pyproject(pyproject)
                        if path.exists():
                            self.load_dir_plugins(path, True)
                    # 如果文件夹内有__init_.py，则视为单个插件包
                    elif plugin_path.exists():
                        importlib.import_module(
                            f'plugins.{plugin.name}.__init__'
                        )
                # 如果发现单文件，则视为单文件插件
                elif plugin.suffix == '.py':
                    importlib.import_module(f'plugins.{plugin.name[:-3]}')

                '''trick'''
                if plugin.stem in ['StarRailUID', 'ArknightsUID']:
                    logger.info('[BAI] 检测是否存在失效仓库...')
                    origin_url = sync_get_plugin_url(plugin)
                    if (
                        origin_url
                        and 'baiqwerdvd' not in origin_url
                        and 'qwerdvd' in origin_url
                    ):
                        logger.warning(f'[BAI] 检测到失效仓库: {origin_url}')
                        new_url = origin_url.replace('qwerdvd', 'baiqwerdvd')
                        logger.success(f'[BAI] 替换新仓库地址成功: {new_url}')
                        sync_change_plugin_url(plugin, new_url)

                '''导入成功'''
                logger.success(f'插件{plugin.stem}导入成功!')
            except Exception as e:  # noqa
                exception = sys.exc_info()
                logger.opt(exception=exception).error(
                    f'加载插件时发生错误: {e}'
                )
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
                    importlib.import_module(f'{_p}')

    async def connect(self, websocket: WebSocket, bot_id: str) -> _Bot:
        await websocket.accept()
        self.active_ws[bot_id] = websocket
        self.active_bot[bot_id] = bot = _Bot(bot_id, websocket)
        logger.info(f'{bot_id}已连接！')
        try:
            _task = [_def() for _def in self.bot_connect_def]
            asyncio.gather(*_task)
        except Exception as e:
            logger.exception(e)
        return bot

    async def disconnect(self, bot_id: str):
        await self.active_ws[bot_id].close(code=1001)
        if bot_id in self.active_ws:
            del self.active_ws[bot_id]
        if bot_id in self.active_bot:
            del self.active_bot[bot_id]
        logger.warning(f'{bot_id}已中断！')

    async def send(self, message: str, bot_id: str):
        await self.active_ws[bot_id].send_text(message)

    async def broadcast(self, message: str):
        for bot_id in self.active_ws:
            await self.send(message, bot_id)

    @classmethod
    def on_bot_connect(cls, func: Callable):
        if func not in cls.bot_connect_def:
            cls.bot_connect_def.add(func)
        return func


def check_pyproject(pyproject: Path):
    with open(pyproject, 'rb') as f:
        file_content = f.read().decode('utf-8')
        if "extend-exclude = '''" in file_content:
            file_content = file_content.replace(
                "extend-exclude = '''", ''
            ).replace("'''", '', 1)
        toml_data = toml.loads(file_content)
    if 'project' in toml_data:
        dependencies = toml_data['project'].get('dependencies')
    elif 'tool' in toml_data and 'poetry' in toml_data['tool']:
        dependencies = toml_data['tool']['poetry'].get('dependencies')
    else:
        dependencies = None

    if isinstance(dependencies, List):
        dependencies = parse_dependency(dependencies)

    if dependencies:
        install_dependencies(dependencies)


def install_dependencies(dependencies: Dict):
    global installed_dependencies
    start_tool = check_start_tool()
    if start_tool == 'pdm':
        result = subprocess.run(
            'pdm run python -m ensurepip',
            capture_output=True,
            text=True,
        )
        # 检查命令执行结果
        if result.returncode != 0:
            logger.warning("PDM中pip环境检查失败。错误信息：")
            logger.warning(result.stderr)
            return
    # 解析依赖项
    for (
        dependency,
        version,
    ) in dependencies.items():
        if (
            installed_dependencies
            and dependency not in installed_dependencies
            and dependency not in ignore_dep
        ):
            logger.info(f'安装依赖 {dependency} 中...')
            result = subprocess.run(
                f'{start_tool} install {dependency}',
                capture_output=True,
                text=True,
            )
            # 检查命令执行结果
            if result.returncode == 0:
                logger.success(f"依赖 {dependency} 安装成功！")
            else:
                logger.warning("依赖安装失败。错误信息：")
                logger.warning(result.stderr)
            installed_dependencies = get_installed_dependencies()


def get_installed_dependencies():
    global installed_dependencies
    installed_packages = pkg_resources.working_set
    installed_dependencies = [package.key for package in installed_packages]


def parse_dependency(dependency: List):
    dep = {}
    for i in dependency:
        dep.update(parse_dependency_string(i))
    return dep


def parse_dependency_string(dependency_string: str):
    pattern = r'([\w\-_\.]+)([<>=!]+)([\w\-_\.]+)'
    matches = re.findall(pattern, dependency_string)

    dependencies = {}
    for match in matches:
        dependency = match[0]
        operator = match[1]
        version = match[2]
        dependencies[dependency] = f"{operator}{version}"

    return dependencies
