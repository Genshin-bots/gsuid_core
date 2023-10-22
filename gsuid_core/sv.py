from __future__ import annotations

import traceback
from pathlib import Path
from functools import wraps
from typing import Dict, List, Tuple, Union, Literal, Callable, Optional

from gsuid_core.bot import Bot
from gsuid_core.models import Event
from gsuid_core.logger import logger
from gsuid_core.trigger import Trigger
from gsuid_core.config import core_config


class SVList:
    def __init__(self):
        self.lst: Dict[str, SV] = {}
        self.detail_lst: Dict[str, List[SV]] = {}

    @property
    def get_lst(self):
        return self.lst


SL = SVList()
config_sv = core_config.get_config('sv')


def modify_func(func):
    @wraps(func)
    async def wrapper(bot: Bot, event: Event):
        try:
            result = await func(bot, event)
        finally:
            instancess = Bot.get_instances()
            mutiply_instances = Bot.get_mutiply_instances()
            if bot.uuid in instancess:
                instancess.pop(bot.uuid)
            if bot.uuid in mutiply_instances and bot.mutiply_tag:
                mutiply_instances.pop(bot.uuid)
        return result

    return wrapper


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
            self.TL: Dict[str, Trigger] = {}
            self.is_initialized = True
            stack = traceback.extract_stack()
            file = stack[-2].filename
            path = Path(file)
            parts = path.parts
            i = parts.index('plugins')
            plugins_name = parts[i + 1]
            if plugins_name not in SL.detail_lst:
                SL.detail_lst[plugins_name] = [self]
            else:
                SL.detail_lst[plugins_name].append(self)

            # 判断sv是否已持久化
            if name in config_sv:
                self.priority = config_sv[name]['priority']
                self.enabled = config_sv[name]['enabled']
                self.pm = config_sv[name]['pm']
                self.black_list = config_sv[name]['black_list']
                self.area = config_sv[name]['area']
                if 'white_list' not in config_sv[name]:
                    self.white_list = white_list
                    self.set(white_list=white_list)
                else:
                    self.white_list = config_sv[name]['white_list']
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
                # 写入
                self.set(
                    priority=priority,
                    enabled=enabled,
                    pm=pm,
                    black_list=black_list,
                    area=area,
                    white_list=white_list,
                )

            if name == '测试开关':
                self.pm = 0
                self.enabled = False

    def set(self, **kwargs):
        for var in kwargs:
            setattr(self, var, kwargs[var])
            if self.name not in config_sv:
                config_sv[self.name] = {}
            config_sv[self.name][var] = kwargs[var]
            core_config.set_config('sv', config_sv)

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
                    logger.info(f'载入{type}触发器【{_k}】!')
                    self.TL[_k] = Trigger(
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
