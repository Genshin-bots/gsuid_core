"""进程内 SQLite 单写者闸门。

``@with_session`` 与 ``db_write_guard`` 共用这一把。
优先级看被装饰函数的定义模块（不是调用方），同一任务可重入。
排队超过 ``GATE_WAIT_S`` 就失败。占锁超时会取消该任务，等它退出后再交接。
占锁或排队超过 ``HOLD_LOG_S`` 会打出 owner 名字，方便下次对照谁占着闸门。
"""

import time
import asyncio
import inspect
from collections import deque
from collections.abc import Callable

# 一条正常写的预算是 10 秒。再留一截给收尾，超时就认为闸门被占死。
GATE_WAIT_S = 20.0
# 还没到抢锁期限就先报名字。测试可改小，不能改变 GATE_WAIT_S 的总等待。
HOLD_LOG_S = 5.0


class WriteGateTimeout(Exception):
    """排队等写闸门超过 ``GATE_WAIT_S``。"""


class _Waiter:
    __slots__ = ("cancel", "fut", "label", "tid")

    def __init__(
        self,
        tid: int,
        fut: asyncio.Future[None],
        cancel: Callable[[], None],
        label: str,
    ) -> None:
        self.tid = tid
        self.fut = fut
        self.cancel = cancel
        self.label = label


def _warn(message: str) -> None:
    from gsuid_core.logger import logger

    logger.warning(message)


class _Hold:
    __slots__ = ("_core", "_gate", "_label")

    def __init__(self, gate: "SqliteWriteGate", core: bool, label: str) -> None:
        self._gate = gate
        self._core = core
        self._label = label

    async def __aenter__(self) -> None:
        await self._gate.enter(self._core, self._label)

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
        self._label = ""
        self._gen = 0
        self._hold_timer: asyncio.TimerHandle | None = None
        self._acquired_at: float | None = None
        self._cancel_owner: Callable[[], None] | None = None
        self._evicting = False
        self._depth = 0
        self._core: deque[_Waiter] = deque()
        self._plugin: deque[_Waiter] = deque()

    def hold(self, *, core: bool, label: str = "") -> _Hold:
        return _Hold(self, core, label)

    async def __aenter__(self) -> "SqliteWriteGate":
        # async with 闸门本身没有函数名，用写出这条 with 的函数名。
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
        label = caller.f_code.co_name if caller is not None else "db_write_guard"
        del frame
        await self.enter(True, label)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        self.leave()

    def _identity(self) -> tuple[int, Callable[[], None]]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("sqlite write gate must run inside a task")

        def _cancel() -> None:
            if not task.done():
                task.cancel()

        return id(task), _cancel

    async def enter(self, core: bool, label: str = "") -> None:
        tid, cancel = self._identity()
        if self._owner == tid:
            # 重入沿用外层名字，避免内层把占锁者改掉。
            self._depth += 1
            return
        if self._owner is None and not self._core and not self._plugin:
            self._take(tid, cancel, label)
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        waiter = _Waiter(tid, fut, cancel, label)
        queue = self._core if core else self._plugin
        queue.append(waiter)
        started = time.monotonic()
        acquired = False
        try:
            while not acquired:
                remaining = GATE_WAIT_S - (time.monotonic() - started)
                if remaining <= 0:
                    break
                step = HOLD_LOG_S if remaining > HOLD_LOG_S else remaining
                done, _pending = await asyncio.wait({fut}, timeout=step)
                if fut in done:
                    acquired = True
                    break
                waited = time.monotonic() - started
                owner = self._label if self._label else "-"
                waiting = label if label else "-"
                _warn(f"[数据库] 写闸门排队 label={waiting} waited={waited:.1f}s owner={owner}")
        except asyncio.CancelledError:
            if self._owner == tid:
                self._handoff()
            else:
                self._discard(fut)
            raise
        if not acquired:
            self._discard(fut)
            # 交接和超时撞在一起时，自己可能已经是 owner，必须先交出去。
            if self._owner == tid:
                self._handoff()
            else:
                self._steal_if_overdue()
            raise WriteGateTimeout()
        if self._owner != tid:
            raise RuntimeError("sqlite write gate woke the wrong task")

    def leave(self) -> None:
        tid, _cancel = self._identity()
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

    def _cancel_hold_timer(self) -> None:
        timer = self._hold_timer
        self._hold_timer = None
        if timer is not None:
            timer.cancel()

    def _arm_hold_log(self) -> None:
        started = self._acquired_at
        if started is None:
            return
        loop = asyncio.get_running_loop()
        generation = self._gen
        label = self._label

        def _tick() -> None:
            # 世代号作废已排队的回调，交接后不会把下一位记成上一位。
            if self._gen != generation:
                return
            held = time.monotonic() - started
            shown = label if label else "-"
            _warn(f"[数据库] 写闸门仍占用 owner={shown} held={held:.1f}s")
            if self._gen != generation:
                return
            self._hold_timer = loop.call_later(HOLD_LOG_S, _tick)

        self._cancel_hold_timer()
        self._hold_timer = loop.call_later(HOLD_LOG_S, _tick)

    def _take(self, tid: int, cancel: Callable[[], None], label: str) -> None:
        self._cancel_hold_timer()
        self._gen += 1
        self._owner = tid
        self._label = label
        self._cancel_owner = cancel
        self._depth = 1
        self._acquired_at = time.monotonic()
        self._evicting = False
        self._arm_hold_log()

    def _steal_if_overdue(self) -> None:
        acquired = self._acquired_at
        if self._evicting or self._owner is None or acquired is None:
            return
        # 留 50ms，避免计时刚好卡在期限上把正常写取消。
        if time.monotonic() - acquired + 0.05 < GATE_WAIT_S:
            return
        from gsuid_core.i18n import t
        from gsuid_core.logger import logger

        shown = self._label if self._label else "-"
        held = time.monotonic() - acquired
        _warn(f"[数据库] 写闸门占用超时，准备取消 owner={shown} held={held:.1f}s")
        budget = str(int(GATE_WAIT_S)) if GATE_WAIT_S.is_integer() else str(GATE_WAIT_S)
        logger.error(t("log.database.write_gate_stolen", budget=budget))
        # 先取消，等占锁任务自己 leave 再交接，避免两个人同时写。
        self._evicting = True
        cancel = self._cancel_owner
        if cancel is not None:
            cancel()

    def _handoff(self) -> None:
        self._cancel_hold_timer()
        self._gen += 1
        self._owner = None
        self._label = ""
        self._cancel_owner = None
        self._depth = 0
        self._acquired_at = None
        self._evicting = False
        waiter = self._pop()
        if waiter is None:
            return
        self._take(waiter.tid, waiter.cancel, waiter.label)
        waiter.fut.set_result(None)


sqlite_write_gate = SqliteWriteGate()
