from __future__ import annotations

import traceback
from pathlib import Path
from copy import deepcopy
from functools import wraps
from typing import Dict, List, Tuple, Union, Literal, Callable, Optional

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.logger import logger
from gsuid_core.trigger import Trigger
from gsuid_core.config import core_config, plugins_sample


class SVList:
    def __init__(self):
        self.lst: Dict[str, SV] = {}
        self.plugins: Dict[str, Plugins] = {}
        self.detail_lst: Dict[Plugins, List[SV]] = {}

    @property
    def get_lst(self):
        return self.lst


SL = SVList()
config_sv = core_config.get_config('sv')
config_plugins = core_config.get_config('plugins')


def modify_func(func):
    @wraps(func)
    async def wrapper(bot: Bot, event: Event):
        try:
            result = await func(bot, event)
        finally:
            instancess = Bot.get_instances()
            mutiply_instances = Bot.get_mutiply_instances()
            mutiply_map = Bot.get_mutiply_map()
            if bot.uuid in instancess:
                instancess.pop(bot.uuid)
            if bot.uuid in mutiply_instances and bot.mutiply_tag:
                mutiply_instances.pop(bot.uuid)
                mutiply_map.pop(bot.gid)
        return result

    return wrapper


class Plugins:
    is_initialized = False

    def __new__(cls, *args, **kwargs):
        # 判断sv是否已经被初始化
        if len(args) >= 1:
            name = args[0]
        else:
            name = kwargs.get('name')

        if name is None:
            raise ValueError('Plugins.name is None!')

        if name in SL.plugins:
            return SL.plugins[name]
        else:
            _plugin = super().__new__(cls)
            SL.plugins[name] = _plugin
            return _plugin

    def __hash__(self) -> int:
        return hash(f'{self.name}{self.pm}{self.area}')

    def __init__(
        self,
        name: str = '',
        pm: int = 6,
        priority: int = 5,
        enabled: bool = True,
        area: Literal['GROUP', 'DIRECT', 'ALL', 'SV'] = 'SV',
        black_list: List = [],
        white_list: List = [],
        sv: Dict = {},
    ):
        self.name = name
        self.priority = priority
        self.enabled = enabled
        self.pm = pm
        self.black_list = black_list
        self.area = area
        self.white_list = white_list


class SV:
    is_initialized = False

    def __new__(cls, *args, **kwargs):
        # 判断sv是否已经被初始化
        if len(args) >= 1:
            name = args[0]
        else:
            name = kwargs.get('name')

        if name is None:
            raise ValueError('SV.name is None!')

        if name in SL.lst:
            return SL.lst[name]
        else:
            _sv = super().__new__(cls)
            SL.lst[name] = _sv
            return _sv

    def __init__(
        self,
        name: str = '',
        pm: int = 6,
        priority: int = 5,
        enabled: bool = True,
        area: Literal['GROUP', 'DIRECT', 'ALL'] = 'ALL',
        black_list: List = [],
        white_list: List = [],
    ):
        if not self.is_initialized:
            logger.info(f'【{name}】模块初始化中...')
            # sv名称，重复的sv名称将被并入一个sv里
            self.name: str = name
            # sv内包含的触发器
            self.TL: Dict[str, Dict[str, Trigger]] = {}
            self.is_initialized = True
            stack = traceback.extract_stack()
            file = stack[-2].filename
            path = Path(file)
            parts = path.parts
            i = parts.index('plugins')
            self.self_plugin_name = plugins_name = parts[i + 1]

            # 初始化
            if plugins_name not in config_plugins:
                _plugins_config = deepcopy(plugins_sample)
                _plugins_config['name'] = plugins_name
                config_plugins[plugins_name] = _plugins_config
                core_config.set_config('plugins', config_plugins)
                plugins = Plugins(**_plugins_config)
            else:
                plugins = Plugins(**config_plugins[plugins_name])

            # SV指向唯一Plugins实例
            self.plugins = plugins

            # 将plugins实例添加到SL.plugins
            if plugins_name not in SL.plugins:
                SL.plugins[plugins_name] = plugins

            if plugins not in SL.detail_lst:
                SL.detail_lst[plugins] = [self]
            else:
                SL.detail_lst[plugins].append(self)

            # 判断sv是否已持久化
            plugin_config = config_plugins[plugins_name]['sv']
            need_write = False
            if name in config_sv:
                self.priority = config_sv[name]['priority']
                self.enabled = config_sv[name]['enabled']
                self.pm = config_sv[name]['pm']
                self.black_list = config_sv[name]['black_list']
                self.area = config_sv[name]['area']
                self.white_list = config_sv[name]['white_list']
                del config_sv[name]
                core_config.set_config('sv', config_sv)
                need_write = True
            elif name in plugin_config:
                self.priority = plugin_config[name]['priority']
                self.enabled = plugin_config[name]['enabled']
                self.pm = plugin_config[name]['pm']
                self.black_list = plugin_config[name]['black_list']
                self.area = plugin_config[name]['area']
                self.white_list = plugin_config[name]['white_list']
            else:
                # sv优先级
                self.priority = priority
                # sv是否开启
                self.enabled = enabled
                # 黑名单群
                self.black_list = black_list
                # 权限 0为master，1为superuser，2为群的群主&管理员，3为普通
                self.pm = pm
                # 作用范围
                self.area = area
                self.white_list = white_list
                need_write = True

            if need_write:
                # 写入
                self.set(
                    priority=self.priority,
                    enabled=self.enabled,
                    pm=self.pm,
                    black_list=self.black_list,
                    area=self.area,
                    white_list=self.white_list,
                )

            if name == '测试开关':
                self.pm = 6
                self.enabled = False

    def set(self, **kwargs):
        plugin_config = config_plugins[self.self_plugin_name]['sv']
        for var in kwargs:
            setattr(self, var, kwargs[var])
            if self.name not in plugin_config:
                plugin_config[self.name] = {}
            plugin_config[self.name][var] = kwargs[var]
        core_config.set_config('plugins', config_plugins)

    def enable(self):
        self.set(enabled=True)

    def disable(self):
        self.set(enabled=False)

    def _on(
        self,
        type: Literal[
            'prefix',
            'suffix',
            'keyword',
            'fullmatch',
            'command',
            'file',
            'regex',
        ],
        keyword: Union[str, Tuple[str, ...]],
        block: bool = False,
        to_me: bool = False,
    ):
        def deco(func: Callable) -> Callable:
            if isinstance(keyword, str):
                keyword_list = (keyword,)
            else:
                keyword_list = keyword

            for _k in keyword_list:
                if _k not in self.TL:
                    logger.trace(f'载入{type}触发器【{_k}】!')
                    if type not in self.TL:
                        self.TL[type] = {}

                    self.TL[type][_k] = Trigger(
                        type, _k, modify_func(func), block, to_me
                    )

            @wraps(func)
            async def wrapper(bot: Bot, msg) -> Optional[Callable]:
                result = await func(bot, msg)
                return result

            return wrapper

        return deco

    def on_fullmatch(
        self,
        keyword: Union[str, Tuple[str, ...]],
        block: bool = False,
        to_me: bool = False,
    ) -> Callable:
        return self._on('fullmatch', keyword, block, to_me)

    def on_prefix(
        self,
        keyword: Union[str, Tuple[str, ...]],
        block: bool = False,
        to_me: bool = False,
    ) -> Callable:
        return self._on('prefix', keyword, block, to_me)

    def on_suffix(
        self,
        keyword: Union[str, Tuple[str, ...]],
        block: bool = False,
        to_me: bool = False,
    ) -> Callable:
        return self._on('suffix', keyword, block, to_me)

    def on_keyword(
        self,
        keyword: Union[str, Tuple[str, ...]],
        block: bool = False,
        to_me: bool = False,
    ) -> Callable:
        return self._on('keyword', keyword, block, to_me)

    def on_command(
        self,
        keyword: Union[str, Tuple[str, ...]],
        block: bool = False,
        to_me: bool = False,
    ) -> Callable:
        return self._on('command', keyword, block, to_me)

    def on_file(
        self,
        file_type: str,
        block: bool = False,
        to_me: bool = False,
    ) -> Callable:
        return self._on('file', file_type, block, to_me)

    def on_regex(
        self,
        keyword: Union[str, Tuple[str, ...]],
        block: bool = False,
        to_me: bool = False,
    ) -> Callable:
        return self._on('regex', keyword, block, to_me)
