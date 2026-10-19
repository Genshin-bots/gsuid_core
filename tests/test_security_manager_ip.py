"""客户端 IP：合法地址、从右向左跳过信任反代、HTTP 与 WebSocket 同一列表。"""

from __future__ import annotations

import pytest
from starlette.types import Scope
from starlette.requests import Request

from gsuid_core.security_manager import (
    sec_manager,
    normalize_ip,
    get_client_ip,
    resolve_client_ip,
    trusted_proxy_ips,
)
from gsuid_core.http_trace_middleware import _client_ip


def _request(peer: str, forwarded: str, real_ip: str) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if forwarded:
        headers.append((b"x-forwarded-for", forwarded.encode("ascii")))
    if real_ip:
        headers.append((b"x-real-ip", real_ip.encode("ascii")))
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": (peer, 1234),
        "server": ("127.0.0.1", 80),
    }
    return Request(scope)


def test_normalize_ip_accepts_mapped_and_expanded_loopback() -> None:
    assert normalize_ip("::ffff:127.0.0.1") == "127.0.0.1"
    assert normalize_ip("::ffff:7f00:1") == "127.0.0.1"
    assert normalize_ip("0:0:0:0:0:0:0:1") == "::1"
    assert normalize_ip("not-an-ip") == ""
    assert normalize_ip("localhost") == ""
    assert normalize_ip("1.1.1.1, 203.0.113.8") == ""


def test_untrusted_peer_ignores_forwarded_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gsuid_core.security_manager.trusted_proxy_ips", lambda: {"127.0.0.1"})
    request = _request("203.0.113.9", "1.1.1.1", "8.8.8.8")
    assert get_client_ip(request) == "203.0.113.9"


def test_forwarded_for_walks_from_the_right(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gsuid_core.security_manager.trusted_proxy_ips",
        lambda: {"127.0.0.1", "198.51.100.10"},
    )
    request = _request("127.0.0.1", "9.9.9.9, 203.0.113.8, 198.51.100.10", "198.51.100.10")
    assert get_client_ip(request) == "203.0.113.8"


def test_real_ip_is_fallback_and_discards_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gsuid_core.security_manager.trusted_proxy_ips", lambda: {"127.0.0.1"})
    garbage = _request("127.0.0.1", "", "not-an-ip")
    assert get_client_ip(garbage) == "127.0.0.1"
    listed = _request("127.0.0.1", "", "1.1.1.1, 203.0.113.8")
    assert get_client_ip(listed) == "203.0.113.8"
    mapped = _request("127.0.0.1", "", "::ffff:203.0.113.4")
    assert get_client_ip(mapped) == "203.0.113.4"


def test_trusted_list_follows_config_and_drops_non_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    current = ["localhost", "::ffff:127.0.0.1"]

    def _read(key: str) -> list[str]:
        assert key == "TRUSTED_IPS"
        return current

    monkeypatch.setattr("gsuid_core.security_manager.core_config.get_config", _read)
    assert trusted_proxy_ips() == {"127.0.0.1"}
    assert sec_manager.is_trusted("127.0.0.1")
    assert sec_manager.is_trusted("::ffff:127.0.0.1")
    assert sec_manager.is_trusted("0:0:0:0:0:0:0:1") is False
    assert not sec_manager.is_trusted("localhost")
    current.remove("::ffff:127.0.0.1")
    current.append("::1")
    assert not sec_manager.is_trusted("127.0.0.1")
    assert sec_manager.is_trusted("0:0:0:0:0:0:0:1")


def test_trace_log_uses_the_same_client_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gsuid_core.security_manager.trusted_proxy_ips", lambda: {"127.0.0.1"})
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/hdr",
        "raw_path": b"/api/hdr",
        "query_string": b"",
        "headers": [
            (b"x-forwarded-for", b"1.1.1.1, 203.0.113.8"),
            (b"x-real-ip", b"not-an-ip"),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }
    assert _client_ip(scope) == resolve_client_ip("127.0.0.1", "1.1.1.1, 203.0.113.8", "not-an-ip")
    assert _client_ip(scope) == "203.0.113.8"
