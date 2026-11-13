"""证据存储：进程内、每会话 LRU，重启丢失只导致保守重查（§4.5）。"""

from __future__ import annotations

import time
import hashlib
from typing import Literal
from collections import OrderedDict
from dataclasses import field, dataclass

Outcome = Literal["data", "auth_required", "error", "action_done"]

_OBS_TTL_SECONDS = 24 * 3600
_STORE_LRU = 200
_INVALIDATION_CAP = 512


@dataclass(frozen=True, slots=True)
class Observation:
    obs_id: str  # "ob_" + 短哈希
    session_id: str
    tool_name: str
    service: str  # MCP config_id 或工具所属插件名
    requested_by: str  # 触发调用的唤醒项说话人 uid
    requested_by_seq: int
    credential_owner: str | None  # 仅 MCP private："shared" 或 uid；否则 None
    outcome: Outcome
    observed_at: float
    summary: str  # ≤ 80 字一行
    handle: str  # to_… 全文句柄，可空


@dataclass(frozen=True, slots=True)
class LiveState:
    service: str
    user_id: str
    aspect: Literal["credential"]
    value: Literal["bound", "unbound"]


def make_obs_id(tool_name: str, requested_by: str, observed_at: float) -> str:
    raw = f"{tool_name}|{requested_by}|{observed_at:.6f}".encode()
    return "ob_" + hashlib.sha1(raw).hexdigest()[:8]


@dataclass(slots=True)
class EvidenceStore:
    """每会话一份；list 保序便于取最近 N 条。"""

    observations: list[Observation] = field(default_factory=list)
    # (service, bot_id, uid) → 凭据变更时间；早于它的 auth_required 观测视为失效
    invalidations: OrderedDict[tuple[str, str, str], float] = field(default_factory=OrderedDict)

    def append(self, obs: Observation) -> None:
        self.observations.append(obs)
        cutoff = time.time() - _OBS_TTL_SECONDS
        self.observations = [o for o in self.observations if o.observed_at >= cutoff]
        if len(self.observations) > _STORE_LRU:
            del self.observations[: len(self.observations) - _STORE_LRU]

    def recent(self, *, limit: int, within_s: float, now: float) -> list[Observation]:
        cutoff = now - within_s
        hits = [o for o in self.observations if o.observed_at >= cutoff]
        hits.sort(key=lambda o: o.observed_at, reverse=True)
        return hits[:limit]

    def find(self, obs_id: str) -> Observation | None:
        for obs in self.observations:
            if obs.obs_id == obs_id:
                return obs
        return None

    def invalidate_credential(self, service: str, bot_id: str, uid: str, at: float) -> None:
        key = (service, bot_id, uid)
        self.invalidations[key] = at
        self.invalidations.move_to_end(key)
        while len(self.invalidations) > _INVALIDATION_CAP:
            self.invalidations.popitem(last=False)

    def is_invalidated(self, obs: Observation, bot_id: str) -> bool:
        if obs.outcome != "auth_required":
            return False
        key = (obs.service, bot_id, obs.credential_owner or obs.requested_by)
        changed_at = self.invalidations[key] if key in self.invalidations else 0.0
        return changed_at > 0 and obs.observed_at < changed_at


_stores: OrderedDict[str, EvidenceStore] = OrderedDict()
_STORES_CAP = 256


def get_evidence_store(session_id: str) -> EvidenceStore:
    if session_id in _stores:
        store = _stores[session_id]
        _stores.move_to_end(session_id)
        return store
    store = EvidenceStore()
    _stores[session_id] = store
    while len(_stores) > _STORES_CAP:
        _stores.popitem(last=False)
    return store


def notify_credential_changed(config_id: str, bot_id: str, uid: str, session_id: str = "") -> None:
    """凭据绑定/解绑/吊销时调用：标记该 uid 该服务的旧 auth_required 观测失效。"""
    from gsuid_core.i18n import t
    from gsuid_core.logger import logger

    now = time.time()
    if session_id:
        get_evidence_store(session_id).invalidate_credential(config_id, bot_id, uid, now)
    else:
        for store in _stores.values():
            store.invalidate_credential(config_id, bot_id, uid, now)
    logger.info(t("log.evidence.credential_changed", service=config_id, uid=uid, bot=bot_id))
