from functools import wraps
from typing import Callable, Optional

from gsuid_core.server import gss
from gsuid_core.logger import logger


def on_bot_connect():
    def deco(func: Callable) -> Callable:
        gss.bot_connect_def.append(func)

        @wraps(func)
        async def wrapper(*args, **kwargs) -> Optional[Callable]:
            logger.info('@on_bot_connect已成功调用...')
            return await func(*args, **kwargs)

        return wrapper

    return deco
