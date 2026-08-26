"""Bearer 鉴权与独立失败封禁。不用 WS_TOKEN / WebConsole 会话。"""

from __future__ import annotations

import time
import hashlib
import ipaddress
from typing import Dict
from dataclasses import dataclass

from fastapi import Request

from gsuid_core.ai_core.http_agent.keys import get_key_store
from gsuid_core.ai_core.http_agent.types import HttpAgentKeyRecord
from gsuid_core.ai_core.http_agent.config import load_http_agent_settings


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
    if not authorization.startswith(prefix):
        return ""
    return authorization[len(prefix) :]


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
        raw = extract_bearer(request.headers.get("authorization"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return f"tok:{digest}"
    return f"ip:{ip}"


def is_ip_banned(ip: str, now: float | None = None) -> bool:
    ts = time.time() if now is None else now
    if ip not in _ip_state:
        return False
    state = _ip_state[ip]
    return state.ban_until > ts


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
    store = get_key_store()
    token = extract_bearer(authorization)
    return store.verify_token(token)


def reset_auth_bans_for_tests() -> None:
    _ip_state.clear()
