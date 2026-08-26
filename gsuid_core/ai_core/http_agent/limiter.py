"""HTTP Agent 独立限流：rpm → 每钥槽 → 本面全局槽。不占 ``_ai_semaphore``。"""

from __future__ import annotations

import time
import asyncio
from typing import Dict, Deque, AsyncIterator
from contextlib import asynccontextmanager
from collections import deque
from dataclasses import field, dataclass

from gsuid_core.ai_core.http_agent.config import load_http_agent_settings


class LimitExceeded(Exception):
    """开流前 429。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _KeySlots:
    used: int = 0
    stamps: Deque[float] = field(default_factory=deque)


class HttpAgentLimiter:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._global_used = 0
        self._keys: Dict[str, _KeySlots] = {}

    def _slot(self, key_id: str) -> _KeySlots:
        if key_id not in self._keys:
            self._keys[key_id] = _KeySlots()
        return self._keys[key_id]

    async def try_acquire(self, key_id: str) -> None:
        settings = load_http_agent_settings()
        now = time.time()
        window_start = now - 60.0
        async with self._lock:
            slot = self._slot(key_id)
            while slot.stamps and slot.stamps[0] < window_start:
                slot.stamps.popleft()
            if len(slot.stamps) >= settings.rate_limit_rpm:
                raise LimitExceeded("rate_limit", "rate limit exceeded")
            if slot.used >= settings.per_key_concurrent:
                raise LimitExceeded("concurrency", "per-key concurrency exceeded")
            if self._global_used >= settings.max_concurrent:
                raise LimitExceeded("concurrency", "global concurrency exceeded")
            slot.used += 1
            self._global_used += 1
            slot.stamps.append(now)

    async def release(self, key_id: str) -> None:
        async with self._lock:
            if key_id not in self._keys:
                if self._global_used > 0:
                    self._global_used -= 1
                return
            slot = self._keys[key_id]
            if slot.used > 0:
                slot.used -= 1
            if self._global_used > 0:
                self._global_used -= 1

    @asynccontextmanager
    async def hold(self, key_id: str) -> AsyncIterator[None]:
        await self.try_acquire(key_id)
        try:
            yield
        finally:
            await self.release(key_id)

    def snapshot(self) -> tuple[int, Dict[str, int]]:
        per_key = {kid: slot.used for kid, slot in self._keys.items()}
        return self._global_used, per_key

    def reset_for_tests(self) -> None:
        self._global_used = 0
        self._keys.clear()


limiter = HttpAgentLimiter()
