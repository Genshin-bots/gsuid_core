from functools import wraps
from typing import Callable, Optional

from bot import Bot
from trigger import TL, Trigger
from model import MessageReceive


class SV:
    def __init__(self, name: str):
        self.name = name

    def on_fullmatch(self, keyword: str) -> Callable:
        def deco(func: Callable) -> Callable:
            triggle = Trigger('fullmatch', keyword, func)
            TL.lst.append(triggle)

            @wraps(func)
            async def wrapper(
                bot: Bot, msg: MessageReceive
            ) -> Optional[Callable]:
                return await func(bot, msg)

            return wrapper

        return deco

    def on_prefix(self, keyword: str) -> Callable:
        def deco(func: Callable) -> Callable:
            triggle = Trigger('prefix', keyword, func)
            TL.lst.append(triggle)

            @wraps(func)
            async def wrapper(
                bot: Bot, msg: MessageReceive
            ) -> Optional[Callable]:
                return await func(bot, msg)

            return wrapper

        return deco

    def on_suffix(self, keyword: str) -> Callable:
        def deco(func: Callable) -> Callable:
            triggle = Trigger('suffix', keyword, func)
            TL.lst.append(triggle)

            @wraps(func)
            async def wrapper(
                bot: Bot, msg: MessageReceive
            ) -> Optional[Callable]:
                return await func(bot, msg)

            return wrapper

        return deco

    def on_keyword(self, keyword: str) -> Callable:
        def deco(func: Callable) -> Callable:
            triggle = Trigger('keyword', keyword, func)
            TL.lst.append(triggle)

            @wraps(func)
            async def wrapper(
                bot: Bot, msg: MessageReceive
            ) -> Optional[Callable]:
                return await func(bot, msg)

            return wrapper

        return deco
