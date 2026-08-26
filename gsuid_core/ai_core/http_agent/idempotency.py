"""``key_id:client_msg_id`` 幂等。进行中/完成 → 开流前 409；断线不重放。"""

from __future__ import annotations

import time
from typing import Dict, Literal
from dataclasses import dataclass

from gsuid_core.ai_core.http_agent.config import load_http_agent_settings

IdempotencyState = Literal["in_progress", "completed"]


@dataclass
class IdempotencyRecord:
    key: str
    state: IdempotencyState
    run_id: str
    created_at: float


class IdempotencyConflict(Exception):
    def __init__(self, rec: IdempotencyRecord) -> None:
        super().__init__("idempotency conflict")
        self.rec = rec


class IdempotencyStore:
    def __init__(self) -> None:
        self._items: Dict[str, IdempotencyRecord] = {}

    def _make_key(self, key_id: str, client_msg_id: str) -> str:
        return f"{key_id}:{client_msg_id}"

    def _prune(self, now: float) -> None:
        settings = load_http_agent_settings()
        ttl = float(settings.idempotency_ttl)
        expired = [k for k, rec in self._items.items() if now - rec.created_at > ttl]
        for k in expired:
            del self._items[k]
        cap = settings.idempotency_cap
        if len(self._items) <= cap:
            return
        ordered = sorted(self._items.values(), key=lambda r: r.created_at)
        drop_n = len(self._items) - cap
        for rec in ordered[:drop_n]:
            if rec.key in self._items:
                del self._items[rec.key]

    def begin(self, key_id: str, client_msg_id: str, run_id: str) -> None:
        now = time.time()
        self._prune(now)
        store_key = self._make_key(key_id, client_msg_id)
        if store_key in self._items:
            raise IdempotencyConflict(self._items[store_key])
        rec = IdempotencyRecord(key=store_key, state="in_progress", run_id=run_id, created_at=now)
        self._items[store_key] = rec

    def complete(self, key_id: str, client_msg_id: str) -> None:
        store_key = self._make_key(key_id, client_msg_id)
        if store_key not in self._items:
            return
        rec = self._items[store_key]
        rec.state = "completed"

    def reset_for_tests(self) -> None:
        self._items.clear()


idempotency_store = IdempotencyStore()
