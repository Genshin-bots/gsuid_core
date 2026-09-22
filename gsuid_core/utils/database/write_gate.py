"""进程内 SQLite 单写者闸门。

``@with_session`` 与 ``db_write_guard`` 共用这一把。
优先级看被装饰函数的定义模块（不是调用方），同一任务可重入。
"""

import asyncio
from collections import deque


class _Waiter:
    __slots__ = ("fut", "tid")

    def __init__(self, tid: int, fut: asyncio.Future[None]) -> None:
        self.tid = tid
        self.fut = fut


class _Hold:
    __slots__ = ("_core", "_gate")

    def __init__(self, gate: "SqliteWriteGate", core: bool) -> None:
        self._gate = gate
        self._core = core

    async def __aenter__(self) -> None:
        await self._gate.enter(self._core)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        self._gate.leave()


class SqliteWriteGate:
    """单写者队列。``async with`` 走框架优先级。"""

    def __init__(self) -> None:
        self._owner: int | None = None
        self._depth = 0
        self._core: deque[_Waiter] = deque()
        self._plugin: deque[_Waiter] = deque()

    def hold(self, *, core: bool) -> _Hold:
        return _Hold(self, core)

    async def __aenter__(self) -> "SqliteWriteGate":
        await self.enter(True)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        self.leave()

    def _tid(self) -> int:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("sqlite write gate must run inside a task")
        return id(task)

    async def enter(self, core: bool) -> None:
        tid = self._tid()
        if self._owner == tid:
            self._depth += 1
            return
        if self._owner is None and not self._core and not self._plugin:
            self._owner = tid
            self._depth = 1
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        waiter = _Waiter(tid, fut)
        queue = self._core if core else self._plugin
        queue.append(waiter)
        try:
            await fut
        except asyncio.CancelledError:
            if self._owner == tid:
                self._handoff()
            else:
                self._discard(fut)
            raise
        if self._owner != tid:
            raise RuntimeError("sqlite write gate woke the wrong task")

    def leave(self) -> None:
        tid = self._tid()
        if self._owner != tid:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        self._handoff()

    def _discard(self, fut: asyncio.Future[None]) -> None:
        self._core = deque(item for item in self._core if item.fut is not fut)
        self._plugin = deque(item for item in self._plugin if item.fut is not fut)

    def _pop(self) -> _Waiter | None:
        for queue in (self._core, self._plugin):
            while queue:
                waiter = queue.popleft()
                if waiter.fut.done():
                    continue
                return waiter
        return None

    def _handoff(self) -> None:
        self._owner = None
        self._depth = 0
        waiter = self._pop()
        if waiter is None:
            return
        self._owner = waiter.tid
        self._depth = 1
        waiter.fut.set_result(None)


sqlite_write_gate = SqliteWriteGate()
