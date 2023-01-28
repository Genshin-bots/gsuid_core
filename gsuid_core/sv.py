from functools import wraps
from typing import Dict, List, Self, Tuple, Union, Literal, Callable, Optional

from bot import Bot
import websockets.server
from trigger import Trigger
from config import core_config
from model import MessageContent, MessageReceive


class SVList:
    def __init__(self):
        self.lst: Dict[str, SV] = {}

    @property
    def get_lst(self):
        return self.lst


SL = SVList()
config_sv = core_config.get_config('sv')
config_masters = core_config.get_config('masters')
config_superusers = core_config.get_config('superusers')


class SV:
    is_initialized = False

    def __new__(cls: type[Self], *args):
        # 判断sv是否已经被初始化
        if args[0] in SL.lst:
            return SL.lst[args[0]]
        else:
            _sv = super().__new__(cls)
            SL.lst[args[0]] = _sv
            return _sv

    def __init__(
        self,
        name: str,
        permission: int = 3,
        priority: int = 5,
        enabled: bool = True,
    ):
        if not self.is_initialized:
            # sv名称，重复的sv名称将被并入一个sv里
            self.name: str = name
            # sv内包含的触发器
            self.TL: List[Trigger] = []
            self.is_initialized = True

            # 判断sv是否已持久化
            if name in config_sv:
                self.priority = config_sv[name]['priority']
                self.enabled = config_sv[name]['enabled']
                self.permission = config_sv[name]['permission']
            else:
                # sv优先级
                self.priority: int = priority
                # sv是否开启
                self.enabled: bool = enabled
                # 权限 0为master，1为superuser，2为群的群主&管理员，3为普通
                self.permission: int = permission
                # 写入
                self.set(
                    priority=priority, enabled=enabled, permission=permission
                )

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
        type: Literal['prefix', 'suffix', 'keyword', 'fullmatch'],
        keyword: Union[str, Tuple[str, ...]],
    ):
        def deco(func: Callable) -> Callable:
            keyword_list = keyword
            if isinstance(keyword, str):
                keyword_list = (keyword,)
            trigger = [Trigger(type, _k, func) for _k in keyword_list]
            self.TL.extend(trigger)

            @wraps(func)
            async def wrapper(
                bot: Bot, msg: MessageReceive
            ) -> Optional[Callable]:
                return await func(bot, msg)

            return wrapper

        return deco

    def on_fullmatch(self, keyword: Union[str, Tuple[str, ...]]) -> Callable:
        return self._on('fullmatch', keyword)

    def on_prefix(self, keyword: Union[str, Tuple[str, ...]]) -> Callable:
        return self._on('keyword', keyword)

    def on_suffix(self, keyword: Union[str, Tuple[str, ...]]) -> Callable:
        return self._on('suffix', keyword)

    def on_keyword(self, keyword: Union[str, Tuple[str, ...]]) -> Callable:
        return self._on('keyword', keyword)


async def get_user_pml(msg: MessageReceive) -> int:
    if msg.user_id in config_masters:
        return 0
    elif msg.user_id in config_superusers:
        return 1
    else:
        return msg.user_pm


async def msg_process(msg: MessageReceive) -> MessageContent:
    message = MessageContent(raw=msg)
    for _msg in msg.content:
        if _msg.type == 'text':
            message.raw_text = _msg.data  # type:ignore
        elif _msg.type == 'at':
            message.at = _msg.data
            message.at_list.append(_msg.data)
        elif _msg.type == 'image':
            message.image = _msg.data
            message.image_list.append(_msg.data)
    return message


async def handle_event(
    ws: websockets.server.WebSocketServerProtocol, msg: MessageReceive
):
    # 获取用户权限，越小越高
    user_pm = await get_user_pml(msg)
    message = await msg_process(msg)
    for sv in SL.lst:
        # 服务启动且权限等级超过服务权限
        if SL.lst[sv].enabled and user_pm <= SL.lst[sv].permission:
            for trigger in SL.lst[sv].TL:
                if trigger.check_command(message):
                    message = await trigger.get_command(message)
                    await trigger.func(ws, message)
                    break
            else:
                await ws.send('已收到消息...')
