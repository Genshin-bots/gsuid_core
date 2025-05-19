import re
import sys
import time
import asyncio
import importlib
import subprocess
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Tuple, Union, Callable

import toml
import pkg_resources
from fastapi import WebSocket

from gsuid_core.bot import _Bot
from gsuid_core.logger import logger
from gsuid_core.config import core_config
from gsuid_core.utils.plugins_update.utils import check_start_tool
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

auto_install_dep: bool = core_plugins_config.get_config('AutoInstallDep').data
auto_update_dep: bool = core_plugins_config.get_config('AutoUpdateDep').data

core_start_def: set[Callable] = set()
core_shutdown_def: set[Callable] = set()
installed_dependencies: Dict[str, str] = {}
_module_cache: Dict[str, ModuleType] = {}
ignore_dep = ['python', 'fastapi', 'pydantic']

PLUGIN_PATH = Path(__file__).parent / 'plugins'
BUILDIN_PLUGIN_PATH = Path(__file__).parent / 'buildin_plugins'

if not PLUGIN_PATH.exists():
    PLUGIN_PATH.mkdir()


def on_core_start(func: Callable):
    if func not in core_start_def:
        core_start_def.add(func)
    return func


def on_core_shutdown(func: Callable):
    if func not in core_shutdown_def:
        core_shutdown_def.add(func)
    return func


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

    def load_dir_plugins(
        self, plugin: Path, plugin_parent: str, nest: bool = False
    ) -> List[Tuple[str, Path, str]]:
        module_list = []
        init_path = plugin / '__init__.py'
        name = plugin.name
        if init_path.exists():
            if str(init_path.parents) not in sys.path:
                sys.path.append(str(init_path.parents))
            module_list.append(
                (
                    f'{plugin_parent}.{name}.{name}.__init__',
                    init_path,
                    'plugin',
                )
            )

        for sub_plugin in plugin.iterdir():
            if sub_plugin.is_dir():
                plugin_path = sub_plugin / '__init__.py'
                if plugin_path.exists():
                    if str(plugin_path.parents) not in sys.path:
                        sys.path.append(str(plugin_path.parents))
                    if nest:
                        _p = f'{plugin_parent}.{name}.{name}.{sub_plugin.name}'
                    else:
                        _p = f'{plugin_parent}.{name}.{sub_plugin.name}'
                    module_list.append(
                        (
                            f'{_p}',
                            plugin_path,
                            'module',
                        )
                    )
        return module_list

    def load_plugin(self, plugin: Union[str, Path, str]):
        if isinstance(plugin, str):
            plugin = PLUGIN_PATH / plugin

        if not plugin.exists():
            logger.warning(f'[更新] ❌ 插件{plugin.name}不存在!')
            return f'❌ 插件{plugin.name}不存在!'

        plugin_parent = plugin.parent.name
        if plugin.stem.startswith('_'):
            return f'插件{plugin.name}包含"_", 跳过加载!'

        # 如果发现文件夹，则视为插件包
        logger.debug(f'🔹 导入{plugin.stem}中...')
        logger.trace('===============')
        try:
            module_list = []
            if plugin.is_dir():
                plugin_path = plugin / '__init__.py'
                plugins_path = plugin / '__full__.py'
                nest_path = plugin / '__nest__.py'
                src_path = plugin / plugin.stem
                # 如果文件夹内有__full_.py，则视为插件包合集
                sys.path.append(str(plugin_path.parents))
                if plugins_path.exists():
                    module_list = self.load_dir_plugins(plugin, plugin_parent)
                elif nest_path.exists() or src_path.exists():
                    path = nest_path.parent / plugin.name
                    pyproject = plugin / 'pyproject.toml'
                    if pyproject.exists:
                        check_pyproject(pyproject)
                    if path.exists():
                        module_list = self.load_dir_plugins(
                            path,
                            plugin_parent,
                            True,
                        )
                # 如果文件夹内有__init_.py，则视为单个插件包
                elif plugin_path.exists():
                    module_list = [
                        (
                            f'{plugin_parent}.{plugin.name}.__init__',
                            plugin_path,
                            'plugin',
                        )
                    ]
            # 如果发现单文件，则视为单文件插件
            elif plugin.suffix == '.py':
                module_list = [
                    (f'{plugin_parent}.{plugin.name[:-3]}', plugin, 'single'),
                ]
            '''导入成功'''
            return module_list
        except Exception as e:  # noqa
            exception = sys.exc_info()
            logger.opt(exception=exception).error(f'加载插件时发生错误: {e}')
            logger.warning(f'❌ 插件{plugin.name}加载失败')
            return f'❌ 插件{plugin.name}加载失败'

    def cached_import(self, module_name: str, filepath: Path, _type: str):
        if module_name in _module_cache:
            return _module_cache[module_name]

        start_time = time.time()
        spec = importlib.util.spec_from_file_location(module_name, filepath)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load spec for {module_name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        end_time = time.time()

        duration = round(end_time - start_time, 2)

        if _type == 'plugin':
            logger.success(f'✅ 插件{filepath.parent.stem}导入成功!')
        elif _type == 'full':
            pass
        elif _type == 'single':
            logger.success(
                f'✅ 插件{filepath.stem}导入成功! 耗时: {duration:.2f}秒'
            )
        else:
            logger.trace(
                f'🌱 模块{filepath.parent.stem}导入成功! 耗时: {duration:.2f}秒'
            )

        _module_cache[module_name] = module
        return module

    async def load_plugins(self):
        logger.info('[GsCore] 开始加载插件...')
        get_installed_dependencies()
        sys.path.append(str(Path(__file__).parents[1]))

        # 提前获取路径列表并过滤
        plug_path_list = [
            p
            for p in list(BUILDIN_PLUGIN_PATH.iterdir())
            + list(PLUGIN_PATH.iterdir())
            if p.is_dir() or (p.is_file() and p.suffix == '.py')
        ]

        all_plugins: List[Tuple[str, Path, str]] = []
        for plugin in plug_path_list:
            d = self.load_plugin(plugin)
            if isinstance(d, str):
                continue
            all_plugins.extend(d)

        for module_name, filepath, _type in all_plugins:
            try:
                self.cached_import(module_name, filepath, _type)
            except Exception as e:
                logger.exception(
                    f'❌ 插件{filepath.stem}导入失败, 错误代码: {e}'
                )
                continue

        '''
        max_workers = min(12, (os.cpu_count() or 1) * 2)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            _ = {
                executor.submit(
                    self.cached_import, module_name, filepath, _type
                ): module_name
                for module_name, filepath, _type in all_plugins
            }
        '''

        core_config.lazy_write_config()
        logger.success('[GsCore] 插件加载完成!')

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

    if auto_install_dep or auto_update_dep:
        if 'project' in toml_data:
            dependencies = toml_data['project'].get('dependencies')
        elif 'tool' in toml_data and 'poetry' in toml_data['tool']:
            dependencies = toml_data['tool']['poetry'].get('dependencies')
        else:
            dependencies = None
    else:
        dependencies = None

    if isinstance(dependencies, List):
        dependencies = parse_dependency(dependencies)
    else:
        dependencies = {}

    if 'project' in toml_data:
        sp_dep = toml_data['project'].get('gscore_auto_update_dep')
        if sp_dep:
            sp_dep = parse_dependency(sp_dep)
            logger.debug('[安装/更新依赖] 特殊依赖列表如下：')
            logger.debug(sp_dep)
            logger.debug('========')
            install_dependencies(sp_dep, True)

    if dependencies:
        if auto_update_dep:
            install_dependencies(dependencies, True)
        else:
            install_dependencies(dependencies, False)


def install_dependencies(dependencies: Dict, need_update: bool = False):
    global installed_dependencies
    to_update = find_dependencies_to_update(
        installed_dependencies, dependencies
    )
    if not to_update:
        logger.debug('[安装/更新依赖] 无需更新依赖！')
        return

    logger.debug(f'[安装/更新依赖] 需更新依赖列表如下：\n{to_update}')

    _tool = check_start_tool()
    start_tool = check_start_tool(True)
    logger.debug(f'[安装/更新依赖] 当前启动工具：{start_tool}')

    if start_tool.startswith('pdm') and False:
        result = subprocess.run(
            'pdm run python -m ensurepip',
            capture_output=True,
            text=True,
            shell=True,
        )
        # 检查命令执行结果
        if result.returncode != 0:
            logger.warning("PDM中pip环境检查失败。错误信息：")
            logger.warning(result.stderr)
            return

    logger.trace(
        f'[安装/更新依赖] 开始安装/更新依赖...模式是否为更新：{need_update}'
    )

    if need_update:
        extra = '-U'
    else:
        extra = ''

    logger.trace('[安装/更新依赖] 需检查依赖列表如下：')
    logger.trace(dependencies)
    logger.trace('========')

    # 解析依赖项
    for (
        dependency,
        _version,
    ) in to_update.items():
        if need_update:
            condi = dependency not in ignore_dep
        else:
            condi = (
                installed_dependencies
                and dependency not in installed_dependencies
                and dependency not in ignore_dep
            )
        logger.trace(
            f'[安装/更新依赖] 检测到依赖 {dependency}, 是否满足安装/更新条件 {condi}'
        )

        if condi:
            version: str = _version.get('required_version', '')
            logger.info(f'[安装/更新依赖] {dependency} 中...')
            CMD = f'{start_tool} install "{dependency}{version}" {extra}'

            retcode = execute_cmd(CMD)
            if retcode != 0:
                logger.warning('[安装/更新依赖] 安装失败（将会重试两次）')
                if _tool != 'python':
                    CMD2 = f'{_tool} run python -m ensurepip'
                    retcode = execute_cmd(CMD2)
                    if retcode == 0:
                        retcode = execute_cmd(CMD)

            if retcode != 0:
                logger.warning('[安装/更新依赖] 安装失败（将会重试一次）')
                if ' python -m' in start_tool:
                    start_tool = start_tool.replace(' python -m', '')
                    CMD = (
                        f'{start_tool} install "{dependency}{version}" {extra}'
                    )
                execute_cmd(CMD)
            installed_dependencies = get_installed_dependencies()


def execute_cmd(CMD: str):
    logger.info(f'[CMD执行] 开始执行：{CMD}')
    result = subprocess.run(
        CMD,
        capture_output=True,
        text=True,
        shell=True,
    )
    # 检查命令执行结果
    if result.returncode == 0:
        logger.success(f"[CMD执行] {CMD} 成功执行!")
    else:
        logger.warning(f"[CMD执行] {CMD}执行失败。错误信息：")
        logger.exception(result.stderr)
    return result.returncode


def get_installed_dependencies():
    global installed_dependencies
    installed_packages = pkg_resources.working_set
    installed_dependencies = {
        package.key: package.version for package in installed_packages
    }
    return installed_dependencies


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


def extract_numeric_version(version):
    # 提取版本中的数字和小数点部分
    numeric_version = re.findall(r'\d+', version)
    return tuple(map(int, numeric_version)) if numeric_version else (0,)


def compare_versions(installed_version, required_version):
    installed_tuple = extract_numeric_version(installed_version)
    required_tuple = extract_numeric_version(
        re.sub(r'[<>=]', '', required_version)
    )

    # 基于符号进行比较
    if "<=" in required_version:
        return installed_tuple <= required_tuple
    elif ">=" in required_version:
        return installed_tuple >= required_tuple
    elif "==" in required_version:
        return installed_tuple == required_tuple
    elif "<" in required_version:
        return installed_tuple < required_tuple
    elif ">" in required_version:
        return installed_tuple > required_tuple
    return False


def find_dependencies_to_update(
    installed_deps: Dict[str, str], required_deps: Dict[str, str]
) -> Dict[str, Dict[str, str]]:
    to_update = {}

    for dep, installed_version in installed_deps.items():
        if dep in required_deps:
            required_version = required_deps[dep]
            if not compare_versions(installed_version, required_version):
                to_update[dep] = {
                    "installed_version": installed_version,
                    "required_version": required_version,
                }

    for dep, version in required_deps.items():
        if dep not in installed_deps:
            to_update[dep] = {
                "installed_version": "not installed",
                "required_version": version,
            }

    return to_update
