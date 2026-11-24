"""HTTP Agent 鉴权、API Key 存储、独立限流与幂等控制。"""

from __future__ import annotations

import hmac
import json
import time
import asyncio
import hashlib
import secrets
import ipaddress
from typing import Dict, List, Deque, Literal, Mapping, AsyncIterator
from pathlib import Path
from contextlib import asynccontextmanager
from collections import deque
from dataclasses import field, dataclass

from fastapi import Request

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.ai_core.http_agent.config import load_http_agent_settings
from gsuid_core.ai_core.http_agent.protocol import (
    KEY_ID_LEN,
    TOKEN_PREFIX,
    HttpAgentKeyPublic,
    HttpAgentKeyRecord,
)

# ── API Key 存储（HMAC-SHA256 + pepper）─────────────────────────

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_SECRET_LEN = 32
_PEPPER_BYTES = 32


class KeyStoreError(Exception):
    """建钥参数非法。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _urlsafe(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def _as_mapping(raw: object) -> Mapping[str, object] | None:
    if isinstance(raw, dict):
        return raw
    return None


def _req_str(d: Mapping[str, object], key: str) -> str | None:
    if key not in d:
        return None
    val = d[key]
    return val if isinstance(val, str) else None


def _req_int(d: Mapping[str, object], key: str, default: int) -> int:
    if key not in d:
        return default
    val = d[key]
    return val if isinstance(val, int) and not isinstance(val, bool) else default


def _req_float(d: Mapping[str, object], key: str, default: float) -> float:
    if key not in d:
        return default
    val = d[key]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return default
    return float(val)


def _req_bool(d: Mapping[str, object], key: str, default: bool) -> bool:
    if key not in d:
        return default
    val = d[key]
    return val if isinstance(val, bool) else default


def _parse_record(raw: object) -> HttpAgentKeyRecord | None:
    d = _as_mapping(raw)
    if d is None:
        return None
    key_id = _req_str(d, "key_id")
    token_hash = _req_str(d, "token_hash")
    user_id = _req_str(d, "user_id")
    bot_id = _req_str(d, "bot_id")
    if key_id is None or token_hash is None or user_id is None or bot_id is None or len(key_id) != KEY_ID_LEN:
        return None
    persona = _req_str(d, "persona")
    label = _req_str(d, "label")
    return HttpAgentKeyRecord(
        key_id=key_id,
        token_hash=token_hash,
        user_id=user_id,
        bot_id=bot_id,
        user_pm=_req_int(d, "user_pm", 6),
        persona=persona if persona is not None else "",
        label=label if label is not None else "",
        created_at=_req_float(d, "created_at", 0.0),
        revoked=_req_bool(d, "revoked", False),
    )


def validate_id_part(name: str, value: str) -> None:
    if not value or ":" in value:
        raise KeyStoreError(f"{name} 不能为空且不得包含 ':'")
    if len(value) > 64:
        raise KeyStoreError(f"{name} 过长")


def public_view(rec: HttpAgentKeyRecord) -> HttpAgentKeyPublic:
    return HttpAgentKeyPublic(
        key_id=rec["key_id"],
        user_id=rec["user_id"],
        bot_id=rec["bot_id"],
        user_pm=rec["user_pm"],
        persona=rec["persona"],
        label=rec["label"],
        created_at=rec["created_at"],
        revoked=rec["revoked"],
    )


class HttpAgentKeyStore:
    """进程内钥表 + 磁盘 JSON；pepper 在 ``*_key`` 文件。"""

    def __init__(self, records_path: Path | None = None) -> None:
        base = get_res_path("ai_core")
        self._path = records_path if records_path is not None else base / "http_agent_keys.json"
        self._pepper_path = self._path.with_name(self._path.name + "_key")
        self._pepper: bytes = b""
        self._keys: Dict[str, HttpAgentKeyRecord] = {}
        self._mtime: float = -1.0
        self._dummy_token = TOKEN_PREFIX + "INVALID0_dummy"
        self._load()

    @property
    def pepper(self) -> bytes:
        self._reload_if_changed()
        return self._pepper

    def dummy_digest(self, token: str) -> str:
        """非法/未知钥仍走 HMAC，避免短路径计时差。"""
        self._reload_if_changed()
        material = token if token else self._dummy_token
        return hmac.new(self._pepper, material.encode("utf-8"), hashlib.sha256).hexdigest()

    def digest_token(self, token: str) -> str:
        self._reload_if_changed()
        return hmac.new(self._pepper, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def get(self, key_id: str) -> HttpAgentKeyRecord | None:
        self._reload_if_changed()
        return self._keys[key_id] if key_id in self._keys else None

    def list_public(self) -> List[HttpAgentKeyPublic]:
        self._reload_if_changed()
        return [public_view(rec) for rec in self._keys.values()]

    def create(
        self,
        *,
        user_id: str,
        bot_id: str,
        user_pm: int = 6,
        persona: str = "",
        label: str = "",
    ) -> tuple[str, HttpAgentKeyRecord]:
        validate_id_part("user_id", user_id)
        validate_id_part("bot_id", bot_id)
        if persona:
            validate_id_part("persona", persona)
        if user_pm < 0 or user_pm > 6:
            raise KeyStoreError("user_pm 须在 0–6")
        self._reload_if_changed()
        key_id = _urlsafe(KEY_ID_LEN)
        while key_id in self._keys:
            key_id = _urlsafe(KEY_ID_LEN)
        secret = _urlsafe(_SECRET_LEN)
        token = f"{TOKEN_PREFIX}{key_id}_{secret}"
        rec = HttpAgentKeyRecord(
            key_id=key_id,
            token_hash=self.digest_token(token),
            user_id=user_id,
            bot_id=bot_id,
            user_pm=user_pm,
            persona=persona,
            label=label,
            created_at=time.time(),
            revoked=False,
        )
        self._keys[key_id] = rec
        self._save()
        logger.info(t("log.ai.http_agent_key_created", key_id=key_id, user_id=user_id))
        return token, rec

    def revoke(self, key_id: str) -> bool:
        self._reload_if_changed()
        if key_id not in self._keys:
            return False
        rec = self._keys[key_id]
        rec["revoked"] = True
        self._keys[key_id] = rec
        self._save()
        logger.info(t("log.ai.http_agent_key_revoked", key_id=key_id))
        return True

    def parse_key_id(self, token: str) -> str | None:
        if not token.startswith(TOKEN_PREFIX):
            return None
        rest = token[len(TOKEN_PREFIX) :]
        if len(rest) < KEY_ID_LEN + 1 or rest[KEY_ID_LEN] != "_":
            return None
        key_id = rest[:KEY_ID_LEN]
        return key_id if len(key_id) == KEY_ID_LEN else None

    def verify_token(self, token: str) -> HttpAgentKeyRecord | None:
        """定宽验钥：始终 HMAC，再用 compare_digest。"""
        self._reload_if_changed()
        key_id = self.parse_key_id(token)
        rec = self._keys[key_id] if key_id is not None and key_id in self._keys else None
        dummy = self.dummy_digest("gsk_missing")
        got = self.digest_token(token if token else self._dummy_token)
        expected = rec["token_hash"] if rec is not None and not rec["revoked"] else dummy
        if not hmac.compare_digest(got, expected):
            return None
        if rec is None or rec["revoked"]:
            return None
        return rec

    def _ensure_pepper(self) -> None:
        if self._pepper_path.exists():
            raw = self._pepper_path.read_bytes().strip()
            try:
                self._pepper = bytes.fromhex(raw.decode("ascii"))
            except (ValueError, UnicodeDecodeError):
                self._pepper = raw
            if len(self._pepper) < 16:
                self._pepper = secrets.token_bytes(_PEPPER_BYTES)
                self._pepper_path.write_text(self._pepper.hex(), encoding="utf-8")
            return
        self._pepper = secrets.token_bytes(_PEPPER_BYTES)
        self._pepper_path.write_text(self._pepper.hex(), encoding="utf-8")

    def _load(self) -> None:
        self._ensure_pepper()
        if not self._path.exists():
            self._keys = {}
            self._mtime = -1.0
            return
        try:
            parsed: object = json.loads(self._path.read_text(encoding="utf-8"))
            self._mtime = self._path.stat().st_mtime
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning(t("log.ai.http_agent_keys_load_fail", e=e))
            self._keys = {}
            return
        keys_raw: object = None
        mapping = _as_mapping(parsed)
        if mapping is not None and "keys" in mapping:
            keys_raw = mapping["keys"]
        elif isinstance(parsed, list):
            keys_raw = parsed
        loaded: Dict[str, HttpAgentKeyRecord] = {}
        if isinstance(keys_raw, list):
            for item in keys_raw:
                rec = _parse_record(item)
                if rec is not None:
                    loaded[rec["key_id"]] = rec
        self._keys = loaded

    def _reload_if_changed(self) -> None:
        try:
            mtime = self._path.stat().st_mtime if self._path.exists() else -1.0
        except OSError:
            return
        if mtime != self._mtime:
            self._load()

    def _save(self) -> None:
        payload = {"keys": list(self._keys.values())}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        try:
            self._mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = time.time()


_store: HttpAgentKeyStore | None = None


def get_key_store() -> HttpAgentKeyStore:
    global _store
    if _store is None:
        _store = HttpAgentKeyStore()
    return _store


# ── Bearer 鉴权与独立失败封禁 ────────────────────────────────────


@dataclass
class _IpAuthState:
    fails: int = 0
    ban_until: float = 0.0


_ip_state: Dict[str, _IpAuthState] = {}


def extract_bearer(authorization: str | None) -> str:
    """只认 ``Authorization: Bearer``；缺头或非 Bearer 返回空串（仍走 dummy HMAC）。"""
    if authorization is None:
        return ""
    prefix = "Bearer "
    return authorization[len(prefix) :] if authorization.startswith(prefix) else ""


def client_ip(request: Request) -> str:
    from gsuid_core.security_manager import get_client_ip

    raw = get_client_ip(request)
    return raw if isinstance(raw, str) and raw else "unknown"


def _is_proxy_shared_ip(ip: str) -> bool:
    # nginx 反代后直连 IP 常是 loopback；按它封禁会掐掉整条代理
    if ip in ("unknown", "localhost", ""):
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return not addr.is_global


def ban_identity(request: Request) -> str:
    ip = client_ip(request)
    if _is_proxy_shared_ip(ip):
        auth_header = request.headers["authorization"] if "authorization" in request.headers else None
        raw = extract_bearer(auth_header)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"tok:{digest}"
    return f"ip:{ip}"


def is_ip_banned(ip: str, now: float | None = None) -> bool:
    ts = time.time() if now is None else now
    return ip in _ip_state and _ip_state[ip].ban_until > ts


def record_auth_failure(ip: str) -> None:
    settings = load_http_agent_settings()
    now = time.time()
    if ip not in _ip_state:
        _ip_state[ip] = _IpAuthState()
    state = _ip_state[ip]
    if state.ban_until > now:
        return
    state.fails += 1
    if state.fails >= settings.auth_fail_max:
        state.ban_until = now + float(settings.auth_ban_sec)


def record_auth_success(ip: str) -> None:
    if ip in _ip_state:
        _ip_state[ip] = _IpAuthState()


def authenticate_bearer(authorization: str | None) -> HttpAgentKeyRecord | None:
    return get_key_store().verify_token(extract_bearer(authorization))


# ── 独立限流（rpm → 每钥槽 → 本面全局槽）────────────────────────


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


limiter = HttpAgentLimiter()


# ── key_id:client_msg_id 幂等存储 ────────────────────────────────

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
        for rec in ordered[: len(self._items) - cap]:
            if rec.key in self._items:
                del self._items[rec.key]

    def begin(self, key_id: str, client_msg_id: str, run_id: str) -> None:
        now = time.time()
        self._prune(now)
        store_key = self._make_key(key_id, client_msg_id)
        if store_key in self._items:
            raise IdempotencyConflict(self._items[store_key])
        self._items[store_key] = IdempotencyRecord(key=store_key, state="in_progress", run_id=run_id, created_at=now)

    def complete(self, key_id: str, client_msg_id: str) -> None:
        store_key = self._make_key(key_id, client_msg_id)
        if store_key in self._items:
            self._items[store_key].state = "completed"


idempotency_store = IdempotencyStore()
