"""幂等 409；断线不重放。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from http_agent_support import (
    make_client,
    patch_settings,
    sample_settings,
    install_chat_mocks,
    reset_http_agent_runtime,
)

from gsuid_core.ai_core.http_agent.keys import reset_key_store_for_tests
from gsuid_core.ai_core.http_agent.limiter import limiter


def setup_function() -> None:
    reset_http_agent_runtime()


def test_duplicate_client_msg_id_409(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    install_chat_mocks(monkeypatch)
    client = make_client()
    headers = {"Authorization": f"Bearer {token}"}
    body = {"text": "hi", "client_msg_id": "same-id"}
    r1 = client.post("/api/v1/agent/chat/stream", json=body, headers=headers)
    assert r1.status_code == 200
    r2 = client.post("/api/v1/agent/chat/stream", json=body, headers=headers)
    assert r2.status_code == 409
    assert r2.json()["code"] == "idempotency_conflict"


def test_rate_limit_does_not_consume_client_msg_id(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True, per_key_concurrent=1))
    install_chat_mocks(monkeypatch)
    asyncio.run(limiter.try_acquire(rec["key_id"]))
    client = make_client()
    headers = {"Authorization": f"Bearer {token}"}
    body = {"text": "hi", "client_msg_id": "retry-me"}
    r429 = client.post("/api/v1/agent/chat/stream", json=body, headers=headers)
    assert r429.status_code == 429
    asyncio.run(limiter.release(rec["key_id"]))
    r_ok = client.post("/api/v1/agent/chat/stream", json=body, headers=headers)
    assert r_ok.status_code == 200


def test_new_client_msg_id_after_done_ok(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    install_chat_mocks(monkeypatch)
    client = make_client()
    headers = {"Authorization": f"Bearer {token}"}
    r1 = client.post("/api/v1/agent/chat/stream", json={"text": "hi", "client_msg_id": "a"}, headers=headers)
    r2 = client.post("/api/v1/agent/chat/stream", json={"text": "hi", "client_msg_id": "b"}, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
