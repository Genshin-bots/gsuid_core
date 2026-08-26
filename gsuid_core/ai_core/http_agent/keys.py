"""HTTP Agent API key 存储：HMAC-SHA256(pepper, 完整 token)，明文只在创建时返回一次。"""

from __future__ import annotations

import hmac
import json
import time
import hashlib
import secrets
from typing import Dict, List, Mapping
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path
from gsuid_core.ai_core.http_agent.types import KEY_ID_LEN, TOKEN_PREFIX, HttpAgentKeyPublic, HttpAgentKeyRecord

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
    if not isinstance(val, str):
        return None
    return val


def _req_int(d: Mapping[str, object], key: str, default: int) -> int:
    if key not in d:
        return default
    val = d[key]
    if isinstance(val, bool) or not isinstance(val, int):
        return default
    return val


def _req_float(d: Mapping[str, object], key: str, default: float) -> float:
    if key not in d:
        return default
    val = d[key]
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        return float(val)
    return default


def _req_bool(d: Mapping[str, object], key: str, default: bool) -> bool:
    if key not in d:
        return default
    val = d[key]
    if isinstance(val, bool):
        return val
    return default


def _parse_record(raw: object) -> HttpAgentKeyRecord | None:
    d = _as_mapping(raw)
    if d is None:
        return None
    key_id = _req_str(d, "key_id")
    token_hash = _req_str(d, "token_hash")
    user_id = _req_str(d, "user_id")
    bot_id = _req_str(d, "bot_id")
    if key_id is None or token_hash is None or user_id is None or bot_id is None:
        return None
    if len(key_id) != KEY_ID_LEN:
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
        if key_id not in self._keys:
            return None
        return self._keys[key_id]

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
        if len(rest) < KEY_ID_LEN + 1:
            return None
        if rest[KEY_ID_LEN] != "_":
            return None
        key_id = rest[:KEY_ID_LEN]
        if len(key_id) != KEY_ID_LEN:
            return None
        return key_id

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


def reset_key_store_for_tests(path: Path | None = None) -> HttpAgentKeyStore:
    global _store
    _store = HttpAgentKeyStore(path)
    return _store
