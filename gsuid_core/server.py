import re
import sys
import time
import asyncio
import inspect
import importlib
import subprocess
import importlib.util
from pathlib import Path
from types import ModuleType
from importlib import metadata
from typing import Set, Dict, List, Tuple, Union, Callable

import toml
from fastapi import WebSocket

try:
    from packaging.requirements import Requirement
except ImportError:
    print("正在安装必要依赖 'packaging'...")
    subprocess.check_call([sys.executable, "-m", "ensurepip"])
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "packaging"]
    )
    from packaging.requirements import Requirement

from gsuid_core.bot import _Bot
from gsuid_core.logger import logger
from gsuid_core.config import core_config
from gsuid_core.utils.plugins_config.gs_config import core_plugins_config

auto_install_dep: bool = core_plugins_config.get_config('AutoInstallDep').data
auto_update_dep: bool = core_plugins_config.get_config('AutoUpdateDep').data

core_start_def: Set[Callable] = set()
core_shutdown_def: Set[Callable] = set()
installed_dependencies: Dict[str, str] = {}
_module_cache: Dict[str, ModuleType] = {}
# 忽略的基础依赖，避免重复检查
ignore_dep = {
    'python',
    'fastapi',
    'pydantic',
    'gsuid-core',
    'toml',
    'packaging',
}

PLUGIN_PATH = Path(__file__).parent / 'plugins'
BUILDIN_PLUGIN_PATH = Path(__file__).parent / 'buildin_plugins'

if not PLUGIN_PATH.exists():
    PLUGIN_PATH.mkdir(parents=True, exist_ok=True)


def normalize_name(name: str) -> str:
    """
    将包名规范化：统一转小写，并将 . _ - 统一替换为 -
    解决 starrail_damage_cal 和 starrail-damage-cal 不匹配的问题
    """
    return re.sub(r"[-_.]+", "-", name).lower()


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
    bot_connect_def: Set[Callable] = set()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(GsServer, cls).__new__(cls)
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
            # fix: 使用 parent 而不是 parents (parents是迭代器)
            # 添加包的父级目录到path，以便可以 import package_name
            parent_path = str(init_path.parent.parent)
            if parent_path not in sys.path:
                sys.path.append(parent_path)

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
                    parent_path = str(plugin_path.parent.parent)
                    if parent_path not in sys.path:
                        sys.path.append(parent_path)

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

    def load_plugin(self, plugin: Union[str, Path]):
        if isinstance(plugin, str):
            plugin = PLUGIN_PATH / plugin

        if not plugin.exists():
            logger.warning(f'[更新] ❌ 插件{plugin.name}不存在!')
            return f'❌ 插件{plugin.name}不存在!'

        plugin_parent = plugin.parent.name
        if plugin.stem.startswith('_'):
            return f'插件{plugin.name}包含"_", 跳过加载!'

        logger.debug(f'🔜 导入{plugin.stem}中...')
        logger.trace('===============')
        try:
            module_list = []
            if plugin.is_dir():
                plugin_path = plugin / '__init__.py'
                plugins_path = plugin / '__full__.py'
                nest_path = plugin / '__nest__.py'
                src_path = plugin / plugin.stem

                # 统一添加路径
                if plugin_path.exists():
                    sys.path.append(str(plugin.parent))

                # 检查依赖
                pyproject = plugin / 'pyproject.toml'
                if pyproject.exists():
                    check_pyproject(pyproject)

                if plugins_path.exists():
                    module_list = self.load_dir_plugins(plugin, plugin_parent)
                elif nest_path.exists() or src_path.exists():
                    path = nest_path.parent / plugin.name
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
            return module_list
        except Exception as e:
            logger.error(f'❌ 插件{plugin.name}加载失败!: {e}')
            return f'❌ 插件{plugin.name}加载失败'

    def cached_import(self, module_name: str, filepath: Path, _type: str):
        if module_name in _module_cache:
            return _module_cache[module_name]

        start_time = time.time()
        spec = importlib.util.spec_from_file_location(module_name, filepath)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load spec for {module_name}")

        module = importlib.util.module_from_spec(spec)
        # 先放入sys.modules，处理循环导入
        sys.modules[module_name] = module

        try:
            spec.loader.exec_module(module)
        except Exception:
            # fix: 如果加载失败，清理 dirty module，防止后续误判已加载
            if module_name in sys.modules:
                del sys.modules[module_name]
            raise

        end_time = time.time()
        duration = round(end_time - start_time, 2)

        if _type == 'plugin':
            logger.success(f'✅ 插件{filepath.parent.stem}导入成功!')
        elif _type == 'single':
            logger.success(
                f'✅ 插件{filepath.stem}导入成功! 耗时: {duration:.2f}秒'
            )
        elif _type != 'full':
            logger.trace(
                f'🌱 模块{filepath.parent.stem}导入成功! 耗时: {duration:.2f}秒'
            )

        _module_cache[module_name] = module
        return module

    async def load_plugins(self, dev_mode: bool = False):
        logger.info('💖 [早柚核心]开始加载插件...')
        refresh_installed_dependencies()
        # fix: path append
        root_path = str(Path(__file__).parents[1])
        if root_path not in sys.path:
            sys.path.append(root_path)

        plug_path_list = [
            p
            for p in list(BUILDIN_PLUGIN_PATH.iterdir())
            + list(PLUGIN_PATH.iterdir())
            if p.is_dir() or (p.is_file() and p.suffix == '.py')
        ]

        all_plugins: List[Tuple[str, Path, str]] = []
        for plugin in plug_path_list:
            if dev_mode and not plugin.name.endswith('-dev'):
                continue

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

        core_config.lazy_write_config()
        logger.success('💖 [早柚核心] 插件加载完成!')

    async def connect(self, websocket: WebSocket, bot_id: str) -> _Bot:
        await websocket.accept()
        self.active_ws[bot_id] = websocket
        self.active_bot[bot_id] = bot = _Bot(bot_id, websocket)
        logger.info(f'{bot_id}已连接！')
        try:
            # fix: 正确处理同步和异步回调，并等待 gather
            tasks = []
            for func in self.bot_connect_def:
                if inspect.iscoroutinefunction(func):
                    tasks.append(func())
                else:
                    # 同步函数直接执行
                    try:
                        func()
                    except Exception as e:
                        logger.error(f"Hooks执行错误: {e}")

            if tasks:
                await asyncio.gather(*tasks)

        except Exception as e:
            logger.exception(e)
        return bot

    async def disconnect(self, bot_id: str):
        if bot_id in self.active_ws:
            try:
                await self.active_ws[bot_id].close(code=1001)
            except Exception:
                pass
            del self.active_ws[bot_id]
        if bot_id in self.active_bot:
            del self.active_bot[bot_id]
        logger.warning(f'{bot_id}已中断！')

    async def send(self, message: str, bot_id: str):
        if bot_id in self.active_ws:
            await self.active_ws[bot_id].send_text(message)

    async def broadcast(self, message: str):
        # 创建任务列表以并发发送
        tasks = [self.send(message, bot_id) for bot_id in self.active_ws]
        if tasks:
            await asyncio.gather(*tasks)

    @classmethod
    def on_bot_connect(cls, func: Callable):
        if func not in cls.bot_connect_def:
            cls.bot_connect_def.add(func)
        return func


def check_pyproject(pyproject: Path):
    try:
        with open(pyproject, 'r', encoding='utf-8') as f:
            file_content = f.read()
            # 保留原有的兼容性替换
            if "extend-exclude = '''" in file_content:
                file_content = file_content.replace(
                    "extend-exclude = '''", ''
                ).replace("'''", '', 1)
            toml_data = toml.loads(file_content)
    except Exception as e:
        logger.error(f"❌ 解析 pyproject.toml 失败: {pyproject}, 错误: {e}")
        return

    if not (auto_install_dep or auto_update_dep):
        return

    dependencies = []
    if 'project' in toml_data:
        dependencies = toml_data['project'].get('dependencies', [])
        sp_dep = toml_data['project'].get('gscore_auto_update_dep', [])
        if sp_dep:
            logger.debug('📄 [安装/更新依赖] 特殊依赖列表如下：')
            logger.debug(sp_dep)
            process_dependencies(sp_dep, update=True)

    elif 'tool' in toml_data and 'poetry' in toml_data['tool']:
        # 处理 Poetry 格式
        poetry_deps = toml_data['tool']['poetry'].get('dependencies', {})
        for k, v in poetry_deps.items():
            # 1. 跳过 python 自身检查
            if k.lower() == "python":
                continue

            # 2. 处理字典格式的复杂依赖 (如: {version = "^1.0", extras = ["opt"]})
            if isinstance(v, dict):
                v = v.get("version", "*")

            # 3. 简单的 Poetry 语法转换 ( ^ -> ~= )
            # Poetry 的 ^ 表示 "Next Major Version"，
            # pip 的 ~= 表示 "Compatible release"
            # 虽然不完全等价，但在安装依赖场景下，转为 ~= 或 >= 能让 pip 读懂
            if isinstance(v, str):
                if v.startswith('^'):
                    v = '~=' + v[1:]

                if v == "*":
                    dependencies.append(k)
                else:
                    dependencies.append(f"{k}{v}")

    if dependencies:
        logger.trace(f"发现依赖: {dependencies}")
        process_dependencies(dependencies, update=auto_update_dep)


def process_dependencies(dependency_list: List[str], update: bool = False):
    """统一处理依赖列表"""
    to_install = []

    # 每次处理前先刷新，确保获取最新状态
    refresh_installed_dependencies()

    for dep_str in dependency_list:
        try:
            req = Requirement(dep_str)
            # 关键修复：使用规范化后的名字进行比对
            req_name = normalize_name(req.name)

            if req_name in ignore_dep:
                continue

            # 检查是否已安装以及版本是否符合
            if req_name not in installed_dependencies:
                # double check: 有时候元数据名字非常怪异，再次遍历检查
                if req_name not in [
                    normalize_name(k) for k in installed_dependencies.keys()
                ]:
                    logger.info(
                        f"[依赖管理] 未安装依赖: {req_name} (原始需求: {req.name})"
                    )
                    to_install.append(dep_str)
                    continue

            # 如果已安装，检查版本
            if update and req_name in installed_dependencies:
                installed_ver = installed_dependencies[req_name]
                if installed_ver not in req.specifier:
                    logger.info(
                        f"[依赖管理] 依赖版本不匹配: {req_name} "
                        f"(当前: {installed_ver}, 需要: {req.specifier})"
                    )
                    to_install.append(dep_str)
                else:
                    logger.trace(
                        f"[依赖管理] {req_name} 已满足 (当前: {installed_ver})"
                    )

        except Exception as e:
            logger.warning(f"无法解析依赖字符串 '{dep_str}': {e}")

    if to_install:
        install_packages(to_install, upgrade=update)
        # 安装完后再次刷新，防止后续逻辑读不到
        refresh_installed_dependencies()


def install_packages(packages: List[str], upgrade: bool = False):
    if not packages:
        return

    logger.info(f'🚀 [安装/更新依赖] 开始安装以下包: {packages}')

    # 定义镜像源列表 (名称, URL)
    # 顺序: 字节 -> 阿里 -> 清华 -> 官方
    mirrors = [
        ("字节源 (Volces)", "https://mirrors.volces.com/pypi/simple/"),
        ("阿里源 (Aliyun)", "https://mirrors.aliyun.com/pypi/simple/"),
        ("清华源 (Tsinghua)", "https://pypi.tuna.tsinghua.edu.cn/simple"),
        ("官方源 (PyPI)", "https://pypi.org/simple"),
    ]

    # 构建基础命令
    base_cmd = [sys.executable, "-m", "pip", "install"]
    if upgrade:
        base_cmd.append("-U")

    # 追加包名
    base_cmd.extend(packages)

    install_success = False

    # 轮询尝试
    for mirror_name, mirror_url in mirrors:
        logger.info(f'⏳ [安装/更新依赖] 正在尝试使用 [{mirror_name}] ...')

        # 组装完整命令，加入 -i 参数
        cmd = base_cmd + ["-i", mirror_url]

        # 有些环境可能需要信任 host，防止 SSL 报错，可选添加:
        # host = mirror_url.split("//")[-1].split("/")[0]
        # cmd.extend(["--trusted-host", host])

        retcode = execute_cmd(cmd)

        if retcode == 0:
            logger.info(f'✅ [安装/更新依赖] 使用 [{mirror_name}] 安装成功!')
            install_success = True
            break  # 安装成功，跳出循环
        else:
            logger.warning(
                f'⚠️ [安装/更新依赖] 使用 [{mirror_name}] 安装失败，准备尝试下一个源...'
            )

    if not install_success:
        logger.error(
            '❌ [安装/更新依赖] 所有源均尝试失败，请检查网络或包名是否正确。'
        )

    # 刷新依赖状态
    refresh_installed_dependencies()


def execute_cmd(cmd_list: List[str]):
    """
    fix: 使用 list 传参且 shell=False，防止命令注入
    """
    cmd_str = " ".join(cmd_list)
    logger.info(f'[CMD执行] {cmd_str}')

    try:
        # shell=False 是安全的默认值
        result = subprocess.run(
            cmd_list, capture_output=True, text=True, shell=False
        )
        if result.returncode == 0:
            logger.success("[CMD执行] 成功!")
            return 0
        else:
            logger.warning(f"[CMD执行] 失败 (Code {result.returncode})")
            logger.warning(f"Stderr: {result.stderr}")
            return result.returncode
    except Exception as e:
        logger.exception(f"[CMD执行] 发生异常: {e}")
        return -1


def refresh_installed_dependencies():
    """获取已安装依赖的包名与版本"""
    # 关键修复：清除 importlib 的目录缓存，否则看不到刚安装的包
    importlib.invalidate_caches()

    global installed_dependencies

    deps = {}
    try:
        # 重新扫描 distribution
        dists = list(metadata.distributions())
        for dist in dists:
            name = dist.metadata.get('Name')
            version = dist.version
            if name:
                # 关键修复：存入字典时也使用规范化名字
                deps[normalize_name(name)] = version
    except Exception as e:
        logger.error(f"读取已安装包列表失败: {e}")

    installed_dependencies = deps
    return installed_dependencies
