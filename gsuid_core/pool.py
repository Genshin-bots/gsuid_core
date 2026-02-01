import asyncio
import functools
from typing import Any, TypeVar, Callable, Awaitable
from typing_extensions import ParamSpec
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

# 定义泛型变量
P = ParamSpec("P")
R = TypeVar("R")

_executor = ProcessPoolExecutor()
_thread_executor = ThreadPoolExecutor()


async def run_in_process(func: Callable[..., R], *args: Any, **kwargs: Any) -> R:
    """
    通用分发函数：将同步函数扔进进程池运行并异步返回结果
    """
    loop = asyncio.get_running_loop()

    if kwargs:
        p_func = functools.partial(func, *args, **kwargs)
        return await loop.run_in_executor(_executor, p_func)

    return await loop.run_in_executor(_executor, func, *args)


def run_in_process_pool(func: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    """
    专用装饰器：把普通耗时函数自动变成异步并在多进程运行。
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, functools.partial(func, *args, **kwargs))

    return wrapper


def run_in_thread_pool(func: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    """
    明确告知 IDE：此装饰器接收一个返回 R 的函数，
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_thread_executor, functools.partial(func, *args, **kwargs))

    return wrapper
