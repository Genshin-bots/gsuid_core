"""disconnect cancel 还槽；跨钥 cancel 404。"""

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

from gsuid_core.ai_core.handle_ai import PassiveChatResult
from gsuid_core.ai_core.http_agent.keys import reset_key_store_for_tests
from gsuid_core.ai_core.http_agent.limiter import limiter
from gsuid_core.ai_core.http_agent.capture_bot import CaptureBot


def setup_function() -> None:
    reset_http_agent_runtime()


def test_disconnect_releases_slot(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True, max_concurrent=1, per_key_concurrent=1))
    install_chat_mocks(monkeypatch)
    started = asyncio.Event()

    async def _slow(*, bot: CaptureBot, event: object, wall_clock: int, run_id: str) -> PassiveChatResult:
        started.set()
        await asyncio.sleep(5)
        return PassiveChatResult("ok")

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.bridge.run_http_agent_turn", _slow)
    client = make_client()
    headers = {"Authorization": f"Bearer {token}"}
    with client.stream(
        "POST",
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "slow1"},
        headers=headers,
    ) as resp:
        assert resp.status_code == 200
        _ = next(resp.iter_bytes())
    used, per_key = limiter.snapshot()
    assert used == 0
    assert all(v == 0 for v in per_key.values())
    # 槽已还，再次占满不应 429
    install_chat_mocks(monkeypatch, send_text="ok")
    r2 = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "after"},
        headers=headers,
    )
    assert r2.status_code != 429


def test_new_stream_preempts_same_session(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True, per_key_concurrent=2, max_concurrent=4))
    install_chat_mocks(monkeypatch)
    called: list[str] = []

    async def _cap(sid: str, *, except_run_id: str | None = None) -> None:
        called.append(f"{sid}:{except_run_id}")

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.routes.cancel_session_runs", _cap)
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "p1", "session_id": "home"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert len(called) == 1
    assert called[0].endswith(":") is False
    assert ":" in called[0]


def test_cross_key_cancel_is_404(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token_a, _a = store.create(user_id="ua", bot_id="bot")
    token_b, _b = store.create(user_id="ub", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    client = make_client()
    r = client.post(
        "/api/v1/agent/runs/does-not-exist/cancel",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert r.status_code == 404
    r2 = client.post(
        "/api/v1/agent/runs/does-not-exist/cancel",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert r2.status_code == 404
