"""两 key_id 不共享 session；body user_id 忽略。"""

from __future__ import annotations

from pathlib import Path

from http_agent_support import (
    make_client,
    patch_settings,
    sample_settings,
    install_chat_mocks,
    reset_http_agent_runtime,
)

from gsuid_core.ai_core.http_agent.keys import reset_key_store_for_tests
from gsuid_core.ai_core.http_agent.types import HttpAgentKeyRecord
from gsuid_core.ai_core.http_agent.session_id import session_id_for_key
from gsuid_core.ai_core.http_agent.capture_bot import CaptureBot


def setup_function() -> None:
    reset_http_agent_runtime()


def test_two_keys_do_not_share_session_id() -> None:
    a = HttpAgentKeyRecord(
        key_id="aaaaaaaa",
        token_hash="x",
        user_id="same-user",
        bot_id="bot",
        user_pm=6,
        persona="",
        label="",
        created_at=0.0,
        revoked=False,
    )
    b = HttpAgentKeyRecord(
        key_id="bbbbbbbb",
        token_hash="y",
        user_id="same-user",
        bot_id="bot",
        user_pm=6,
        persona="",
        label="",
        created_at=0.0,
        revoked=False,
    )
    sa = session_id_for_key(a, "default")
    sb = session_id_for_key(b, "default")
    assert sa != sb
    assert ":aaaaaaaa_default:" in sa
    assert ":bbbbbbbb_default:" in sb
    assert sa.endswith(":private:same-user")


def test_same_group_shared_across_keys_same_bot() -> None:
    a = HttpAgentKeyRecord(
        key_id="aaaaaaaa",
        token_hash="x",
        user_id="u1",
        bot_id="bot",
        user_pm=6,
        persona="",
        label="",
        created_at=0.0,
        revoked=False,
    )
    b = HttpAgentKeyRecord(
        key_id="bbbbbbbb",
        token_hash="y",
        user_id="u2",
        bot_id="bot",
        user_pm=6,
        persona="",
        label="",
        created_at=0.0,
        revoked=False,
    )
    ga = session_id_for_key(a, "default", "room1")
    gb = session_id_for_key(b, "default", "room1")
    assert ga == gb
    assert ":g_default:group:room1" in ga
    other_bot = HttpAgentKeyRecord(
        key_id="cccccccc",
        token_hash="z",
        user_id="u3",
        bot_id="other",
        user_pm=6,
        persona="",
        label="",
        created_at=0.0,
        revoked=False,
    )
    assert session_id_for_key(other_bot, "default", "room1") != ga
    assert session_id_for_key(a, "tab2", "room1") != ga
    assert session_id_for_key(a, "default") != ga


def test_body_user_id_ignored(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, rec = store.create(user_id="key-user", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    install_chat_mocks(monkeypatch)
    captured: list[str] = []

    async def _turn(*, bot: CaptureBot, event: object, wall_clock: int, run_id: str) -> object:
        from gsuid_core.models import Event
        from gsuid_core.ai_core.handle_ai import PassiveChatResult

        assert isinstance(event, Event)
        captured.append(event.user_id)
        await bot.send("ok")
        return PassiveChatResult("ok")

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.bridge.run_http_agent_turn", _turn)
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "m1", "user_id": "forged-user"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert captured == ["key-user"]
    assert rec["user_id"] == "key-user"


def test_sessions_reset_ok(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    client = make_client()
    r = client.post(
        "/api/v1/agent/sessions/reset",
        json={"session_id": "default"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
