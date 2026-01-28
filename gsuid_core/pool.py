import asyncio
import functools
from concurrent.futures import ProcessPoolExecutor

# 全局进程池
_executor = ProcessPoolExecutor()


def run_in_process_pool(func):
    """
    专用装饰器：把【不包含 bot 对象、不包含 await】的普通耗时函数
    自动变成异步并在多进程运行
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, functools.partial(func, *args, **kwargs))

    return wrapper
