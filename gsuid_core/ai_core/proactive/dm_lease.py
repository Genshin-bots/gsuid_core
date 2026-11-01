"""私聊契约租约管理（DM Affinity Lease）。

保证主动私聊发信后 72 小时内，用户私聊回复路由至原人格，避免回复黑洞与身份漂移。
"""

from __future__ import annotations

import re
import json
import time
from typing import TypedDict
from pathlib import Path
from threading import Lock
from dataclasses import dataclass

from gsuid_core.data_store import AI_CORE_PATH, get_res_path

DM_LEASE_TTL_SECONDS: float = 72 * 3600.0  # 默认 72 小时有效期
_LOCK = Lock()


class DMAffinityLeaseDict(TypedDict):
    persona_name: str
    source_group_id: str
    issued_at: float
    expires_at: float


@dataclass(slots=True)
class DMAffinityLease:
    persona_name: str
    source_group_id: str
    issued_at: float
    expires_at: float


_cached_path: Path | None = None
_cached_mtime: float = -1.0
_cached_leases: dict[str, DMAffinityLease] = {}


def _path() -> Path:
    return get_res_path(AI_CORE_PATH) / "dm_leases.json"


def _make_key(bot_id: str, user_id: str) -> str:
    return f"{bot_id}:{user_id}" if bot_id else user_id


def _load_locked() -> dict[str, DMAffinityLease]:
    global _cached_path, _cached_mtime, _cached_leases
    path = _path()
    if not path.is_file():
        _cached_path = path
        _cached_mtime = -1.0
        _cached_leases = {}
        return {}

    try:
        mtime = path.stat().st_mtime
    except OSError:
        return dict(_cached_leases)

    now = time.time()
    # 路径一致且修改时间未变时直接复用内存缓存，避免高频全量读盘
    if path == _cached_path and mtime == _cached_mtime:
        valid = {k: v for k, v in _cached_leases.items() if v.expires_at > now}
        if len(valid) != len(_cached_leases):
            _cached_leases = valid
            _save_locked(valid)
        return dict(valid)

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    leases_raw = raw["leases"] if "leases" in raw and isinstance(raw["leases"], dict) else {}
    valid: dict[str, DMAffinityLease] = {}
    for key, item in leases_raw.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            continue
        pname = item["persona_name"] if "persona_name" in item else None
        grp = item["source_group_id"] if "source_group_id" in item else ""
        issued = item["issued_at"] if "issued_at" in item else None
        expires = item["expires_at"] if "expires_at" in item else None
        if not isinstance(pname, str) or not pname.strip():
            continue
        if not isinstance(grp, str) or not isinstance(issued, (int, float)) or not isinstance(expires, (int, float)):
            continue
        if float(expires) <= now:
            continue
        valid[key] = DMAffinityLease(
            persona_name=pname,
            source_group_id=grp,
            issued_at=float(issued),
            expires_at=float(expires),
        )
    _cached_path = path
    _cached_mtime = mtime
    _cached_leases = valid
    return dict(valid)


def _save_locked(leases: dict[str, DMAffinityLease]) -> None:
    global _cached_path, _cached_mtime, _cached_leases
    path = _path()
    payload = {
        "leases": {
            k: {
                "persona_name": v.persona_name,
                "source_group_id": v.source_group_id,
                "issued_at": v.issued_at,
                "expires_at": v.expires_at,
            }
            for k, v in leases.items()
        }
    }
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    _cached_path = path
    _cached_leases = dict(leases)
    try:
        _cached_mtime = path.stat().st_mtime
    except OSError:
        _cached_mtime = -1.0


def grant_dm_lease(
    user_id: str,
    persona_name: str,
    bot_id: str = "",
    source_group_id: str = "",
    ttl_seconds: float = DM_LEASE_TTL_SECONDS,
) -> DMAffinityLease:
    """为私聊签发指定人格的亲和契约租约。"""
    now = time.time()
    lease = DMAffinityLease(
        persona_name=persona_name.strip(),
        source_group_id=source_group_id.strip(),
        issued_at=now,
        expires_at=now + ttl_seconds,
    )
    with _LOCK:
        leases = _load_locked()
        key = _make_key(bot_id, user_id)
        leases[key] = lease
        if bot_id:
            leases[user_id] = lease
        _save_locked(leases)
    return lease


def get_active_dm_lease(user_id: str, bot_id: str = "") -> DMAffinityLease | None:
    """查询指定私聊用户的活跃契约租约，已过期返回 None。"""
    with _LOCK:
        leases = _load_locked()
        candidates = [_make_key(bot_id, user_id)] if bot_id else []
        candidates.append(user_id)
        for cand in candidates:
            if cand in leases:
                return leases[cand]
    return None


def renew_dm_lease(
    user_id: str,
    bot_id: str = "",
    ttl_seconds: float = DM_LEASE_TTL_SECONDS,
) -> bool:
    """用户回复时自动延长契约有效期。同时更新所有候选键以保持一致。"""
    with _LOCK:
        leases = _load_locked()
        candidates = [_make_key(bot_id, user_id)] if bot_id else []
        candidates.append(user_id)
        matching_keys = [cand for cand in candidates if cand in leases]
        if not matching_keys:
            return False
        now = time.time()
        base = leases[matching_keys[0]]

        from gsuid_core.ai_core.persona.config import persona_config_manager

        active_configs = persona_config_manager.get_all_configs()
        if active_configs and base.persona_name not in active_configs:
            # 人格已删除，主动清理僵尸租约，不予续期
            for k in matching_keys:
                del leases[k]
            _save_locked(leases)
            return False

        now = time.time()
        new_lease = DMAffinityLease(
            persona_name=base.persona_name,
            source_group_id=base.source_group_id,
            issued_at=base.issued_at,
            expires_at=now + ttl_seconds,
        )
        for k in matching_keys:
            leases[k] = new_lease

        # 距上次落盘衰减超 5 分钟或自定义 TTL 时写盘，合并短时间连续发信写开销
        should_save = (ttl_seconds != DM_LEASE_TTL_SECONDS) or (base.expires_at - now <= ttl_seconds - 300.0)
        if should_save:
            _save_locked(leases)
        else:
            _cached_leases.update(leases)
        return True


def revoke_dm_lease(user_id: str, bot_id: str = "") -> bool:
    """撤销私聊契约。"""
    with _LOCK:
        leases = _load_locked()
        candidates = [_make_key(bot_id, user_id)] if bot_id else []
        candidates.append(user_id)
        revoked = False
        for cand in candidates:
            if cand in leases:
                del leases[cand]
                revoked = True
        if revoked:
            _save_locked(leases)
        return revoked


_PERSONA_NAME_MIN_LEN = 2
_LEADING_STRIP_RE = re.compile(r"^[\s\W_]+")
_ASCII_WORD_TAIL = re.compile(r"[A-Za-z0-9_]")


def maybe_switch_dm_lease(user_id: str, bot_id: str, text: str, current_persona: str | None) -> str | None:
    """私聊开头显式点名其他启用人格时切换租约。返回切换后的人格名，否则 None。

    只对**消息开头**（剥掉前导空白/标点后）的人格目录名（长度 >= 2）生效；
    命中者必须已启用（scope != "disabled"）且不是当前人格。
    名字后紧跟 ASCII 字母/数字/下划线视为同词延续（Beta2 ≠ Beta）；
    中文不受此限——「早柚在吗」这种不带标点的称呼是常态用法。
    """
    body = _LEADING_STRIP_RE.sub("", text or "")
    if not body:
        return None
    from gsuid_core.ai_core.persona.config import persona_config_manager

    for name in sorted(persona_config_manager.get_all_configs(), key=lambda n: (-len(n), n)):
        if name == current_persona or len(name) < _PERSONA_NAME_MIN_LEN:
            continue
        if not body.startswith(name):
            continue
        tail = body[len(name) :]
        if tail and _ASCII_WORD_TAIL.match(tail[0]):
            continue
        config = persona_config_manager.get_config(name)
        if config.get_config("scope").data == "disabled":
            continue
        grant_dm_lease(user_id=user_id, persona_name=name, bot_id=bot_id)
        return name
    return None
