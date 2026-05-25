import os
import sys
import signal
import asyncio
import inspect
import functools
import importlib
import logging
from typing import Any, TypeVar, Callable, Awaitable, Coroutine, ParamSpec, cast, overload
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor

T = TypeVar("T")
P = ParamSpec("P")


def _process_pool_initializer() -> None:
    # fork worker 继承父进程已 bind 的监听 socket; 父进程被 kill 后内核 SIGKILL 本 worker, 防残留占端口
    if sys.platform != "linux":
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        libc.prctl.restype = ctypes.c_int
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
    except Exception as e:
        logging.getLogger("gsuid_core").warning(f"[pool] PR_SET_PDEATHSIG 设置失败: {e}")


_executor = ThreadPoolExecutor(max_workers=10)
_process_executor = ProcessPoolExecutor(initializer=_process_pool_initializer)


def shutdown_pools() -> None:
    for ex in (_process_executor, _executor):
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


try:
    from gsuid_core.server import on_core_shutdown

    on_core_shutdown(shutdown_pools)
except Exception:
    pass


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
