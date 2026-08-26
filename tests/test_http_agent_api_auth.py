"""fail-closed；定宽 Bearer；禁 query；封禁重置。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from http_agent_support import make_agent_app, patch_settings, sample_settings, reset_http_agent_runtime

from gsuid_core.ai_core.http_agent.keys import reset_key_store_for_tests


def setup_function() -> None:
    reset_http_agent_runtime()


def _enable(monkeypatch, tmp_path: Path) -> TestClient:
    reset_key_store_for_tests(tmp_path / "http_agent_keys.json")
    patch_settings(monkeypatch, sample_settings(enable=True, auth_fail_max=3, auth_ban_sec=60))
    return TestClient(make_agent_app())


def test_no_key_is_401_not_open(monkeypatch, tmp_path: Path) -> None:
    client = _enable(monkeypatch, tmp_path)
    r = client.post("/api/v1/agent/chat/stream", json={"text": "hi", "client_msg_id": "m1"})
    assert r.status_code == 401
    body = r.json()
    assert body["code"] == "unauthorized"


def test_wrong_key_401_same_body(monkeypatch, tmp_path: Path) -> None:
    client = _enable(monkeypatch, tmp_path)
    r1 = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "m1"},
        headers={"Authorization": "Bearer gsk_abcd1234_nope"},
    )
    r2 = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "m2"},
        headers={"Authorization": "Token abc"},
    )
    assert r1.status_code == 401
    assert r2.status_code == 401
    assert r1.json()["code"] == r2.json()["code"] == "unauthorized"


def test_query_token_ignored(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "http_agent_keys.json")
    token, _rec = store.create(user_id="u1", bot_id="b1")
    patch_settings(monkeypatch, sample_settings(enable=True))
    client = TestClient(make_agent_app())
    r = client.post(
        f"/api/v1/agent/chat/stream?token={token}",
        json={"text": "hi", "client_msg_id": "m1"},
    )
    assert r.status_code == 401


def test_loopback_failures_do_not_ban_other_keys(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "http_agent_keys.json")
    token, _rec = store.create(user_id="u1", bot_id="b1")
    patch_settings(monkeypatch, sample_settings(enable=True, auth_fail_max=2, auth_ban_sec=60))
    client = TestClient(make_agent_app())
    bad = {"Authorization": "Bearer gsk_zzzzzzzz_x"}
    ok = {"Authorization": f"Bearer {token}"}
    client.post("/api/v1/agent/chat/stream", json={"text": "hi", "client_msg_id": "a"}, headers=bad)
    client.post("/api/v1/agent/chat/stream", json={"text": "hi", "client_msg_id": "b"}, headers=bad)
    still = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "ok2"},
        headers=ok,
    )
    assert still.status_code != 401


def test_shared_vs_public_ban_identity() -> None:
    from gsuid_core.ai_core.http_agent.auth import _is_proxy_shared_ip

    assert _is_proxy_shared_ip("127.0.0.1") is True
    assert _is_proxy_shared_ip("::1") is True
    assert _is_proxy_shared_ip("10.0.0.1") is True
    assert _is_proxy_shared_ip("testclient") is True
    assert _is_proxy_shared_ip("8.8.8.8") is False


def test_public_ip_failures_ban_all_keys(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "http_agent_keys.json")
    token, _rec = store.create(user_id="u1", bot_id="b1")
    patch_settings(monkeypatch, sample_settings(enable=True, auth_fail_max=2, auth_ban_sec=60))
    monkeypatch.setattr(
        "gsuid_core.ai_core.http_agent.routes.ban_identity",
        lambda _r: "ip:8.8.8.8",
    )
    client = TestClient(make_agent_app())
    bad = {"Authorization": "Bearer gsk_zzzzzzzz_x"}
    ok = {"Authorization": f"Bearer {token}"}
    client.post("/api/v1/agent/chat/stream", json={"text": "hi", "client_msg_id": "a"}, headers=bad)
    client.post("/api/v1/agent/chat/stream", json={"text": "hi", "client_msg_id": "b"}, headers=bad)
    banned = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "ok2"},
        headers=ok,
    )
    assert banned.status_code == 401


def test_auth_ban_resets_on_success(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "http_agent_keys.json")
    token, _rec = store.create(user_id="u1", bot_id="b1")
    patch_settings(monkeypatch, sample_settings(enable=True, auth_fail_max=2, auth_ban_sec=60))
    client = TestClient(make_agent_app())
    bad = {"Authorization": "Bearer gsk_zzzzzzzz_x"}
    ok = {"Authorization": f"Bearer {token}"}
    client.post("/api/v1/agent/chat/stream", json={"text": "hi", "client_msg_id": "a"}, headers=bad)
    # 成功把失败计数清零，再失败一次不应封禁
    chat = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "ok1"},
        headers=ok,
    )
    assert chat.status_code != 401
    client.post("/api/v1/agent/chat/stream", json={"text": "hi", "client_msg_id": "b"}, headers=bad)
    still = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "ok2"},
        headers=ok,
    )
    assert still.status_code != 401
