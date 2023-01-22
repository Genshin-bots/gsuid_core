from functools import wraps
from typing import Dict, List, Self, Tuple, Union, Literal, Callable, Optional

from bot import Bot
import websockets.server
from trigger import Trigger
from model import MessageReceive


class SVList:
    def __init__(self):
        self.lst: Dict[str, SV] = {}

    @property
    def get_lst(self):
        return self.lst


SL = SVList()


class SV:
    is_initialized = False

    def __new__(cls: type[Self], *args):
        if args[0] in SL.lst:
            return SL.lst[args[0]]
        else:
            _sv = super().__new__(cls)
            SL.lst[args[0]] = _sv
            return _sv

    def __init__(self, name: str):
        if not self.is_initialized:
            self.name: str = name
            self.priority: int = 5
            self.enabled: bool = True
            self.TL: List[Trigger] = []
            self.is_initialized = True

    def enable(self):
        self.enabled = True

    def disable(self):
        self.enabled = False

    def _on(
        self,
        type: Literal['prefix', 'suffix', 'keyword', 'fullmatch'],
        keyword: Union[str, Tuple[str]],
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

    def on_fullmatch(self, keyword: Union[str, Tuple[str]]) -> Callable:
        return self._on('fullmatch', keyword)

    def on_prefix(self, keyword: Union[str, Tuple[str]]) -> Callable:
        return self._on('keyword', keyword)

    def on_suffix(self, keyword: Union[str, Tuple[str]]) -> Callable:
        return self._on('suffix', keyword)

    def on_keyword(self, keyword: Union[str, Tuple[str]]) -> Callable:
        return self._on('keyword', keyword)


async def handle_event(
    ws: websockets.server.WebSocketServerProtocol, msg: MessageReceive
):
    for sv in SL.lst:
        if SL.lst[sv].enabled:
            for trigger in SL.lst[sv].TL:
                if trigger.check_command(msg):
                    await trigger.func(ws, msg)
                    break
            else:
                await ws.send('已收到消息...')
