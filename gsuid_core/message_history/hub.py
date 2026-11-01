"""会话管理用的实时广播：HistoryManager 写一条就推给所有订阅者。

控制台那页走 WebSocket 订阅，不需要轮询。这里只做「一个写者、多个读者」的最简实现：

- 订阅方拿到一个 ``asyncio.Queue``，满了就丢新的（宁可少一条实时消息，也不堵住发消息主链路）；
- ``publish`` 可以在任意线程调用（``add_message`` 有可能从线程池里进来），
  内部统一丢回事件循环线程再分发。

没有订阅者时 publish 直接返回，不建任务、不留缓存。
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Tuple, Optional

# 每个连接的队列上限。会话页一次拉全量历史，实时只补增量，128 条足够覆盖一次突发
_QUEUE_MAX = 128


class HistoryHub:
    def __init__(self) -> None:
        self._subs: Dict[int, asyncio.Queue] = {}
        self._seq = 0
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """在启动阶段绑定主事件循环，之后 publish 才能跨线程投递。"""
        self._loop = loop

    def subscribe(self) -> Tuple[int, "asyncio.Queue[Dict[str, Any]]"]:
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=_QUEUE_MAX)
        self._seq += 1
        self._subs[self._seq] = queue
        return self._seq, queue

    def unsubscribe(self, sub_id: int) -> None:
        self._subs.pop(sub_id, None)

    def publish(self, payload: Dict[str, Any]) -> None:
        """线程安全。没有订阅者时什么都不做。"""
        if not self._subs:
            return
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._fanout, payload)
        except RuntimeError:
            # 循环正在关闭，丢掉这一条即可
            pass

    def _fanout(self, payload: Dict[str, Any]) -> None:
        for queue in list(self._subs.values()):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # 慢客户端丢新消息，不阻塞别的订阅者
                continue


_hub: Optional[HistoryHub] = None


def get_history_hub() -> HistoryHub:
    global _hub
    if _hub is None:
        _hub = HistoryHub()
    return _hub
