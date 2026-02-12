import asyncio
import inspect
import functools
import importlib
from typing import Any, TypeVar, Callable, Awaitable, Coroutine, ParamSpec, cast, overload
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

T = TypeVar("T")
P = ParamSpec("P")

_executor = ThreadPoolExecutor(max_workers=10)
_process_executor = ProcessPoolExecutor()


@overload
def to_thread(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Awaitable[T]]: ...


@overload
def to_thread(func: Callable[P, T]) -> Callable[P, Awaitable[T]]: ...


def to_thread(func: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()

        def sync_worker():
            # 运行时判断
            if inspect.iscoroutinefunction(func):
                new_loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(new_loop)
                    return new_loop.run_until_complete(func(*args, **kwargs))
                finally:
                    new_loop.close()
            else:
                return func(*args, **kwargs)

        return await loop.run_in_executor(_executor, sync_worker)

    return cast(Callable[..., Awaitable[Any]], wrapper)


run_in_thread_pool = to_thread


def _process_worker(func_path: str, *args: Any, **kwargs: Any) -> Any:
    module_name, func_name = func_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    real_func = getattr(module, func_name)

    if inspect.iscoroutinefunction(real_func):
        return asyncio.run(real_func(*args, **kwargs))
    else:
        return real_func(*args, **kwargs)


@overload
def to_process(func: Callable[P, Coroutine[Any, Any, T]]) -> Callable[P, Awaitable[T]]: ...


@overload
def to_process(func: Callable[P, T]) -> Callable[P, Awaitable[T]]: ...


def to_process(func: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
    func_path = f"{func.__module__}.{func.__qualname__}"

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
        loop = asyncio.get_running_loop()

        return await loop.run_in_executor(
            _process_executor, functools.partial(_process_worker, func_path, *args, **kwargs)
        )

    return cast(Callable[..., Awaitable[Any]], wrapper)
