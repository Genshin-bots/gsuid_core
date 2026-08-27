"""SSE 编码、心跳 comment、无 Connection 头。"""

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
from gsuid_core.ai_core.http_agent.protocol import SSE_HEADERS, encode_sse, encode_comment, parse_sse_chunk
from gsuid_core.ai_core.http_agent.capture_bot import CaptureBot


def test_sse_encode_and_parse() -> None:
    raw = encode_sse("run.start", {"run_id": "abc", "seq": 1}, 1)
    frames = parse_sse_chunk(raw)
    assert len(frames) == 1
    assert frames[0].event == "run.start"
    assert frames[0].id == 1
    assert frames[0].data["run_id"] == "abc"


def test_heartbeat_is_comment_not_event() -> None:
    raw = encode_comment("ping")
    assert raw.startswith(": ")
    assert "event:" not in raw
    assert parse_sse_chunk(raw) == []


def test_sse_headers_have_no_connection() -> None:
    assert "Connection" not in SSE_HEADERS
    assert SSE_HEADERS["Cache-Control"] == "no-cache"
    assert SSE_HEADERS["X-Accel-Buffering"] == "no"


def setup_function() -> None:
    reset_http_agent_runtime()


def test_stream_emits_start_text_one_terminal(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    install_chat_mocks(monkeypatch, send_text="gated-line")
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "p1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert "Connection" not in r.headers
    frames = parse_sse_chunk(r.text)
    events = [f.event for f in frames]
    assert events[0] == "run.start"
    assert "text" in events
    terminals = [e for e in events if e in ("run.done", "run.error")]
    assert len(terminals) == 1
    texts = [f.data["text"] for f in frames if f.event == "text"]
    assert "gated-line" in texts


def test_stream_first_visible_is_text_not_attachment(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    install_chat_mocks(monkeypatch, send_text="")

    async def _turn(*, bot: object, event: object, wall_clock: int, run_id: str) -> object:
        from gsuid_core.ai_core.handle_ai import PassiveChatResult

        send = getattr(bot, "send")
        await send("base64://QQ==")
        return PassiveChatResult("ok")

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.bridge.run_http_agent_turn", _turn)
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "att1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    frames = parse_sse_chunk(r.text)
    visible = [f.event for f in frames if f.event in ("text", "attachment")]
    assert visible, frames
    assert visible[0] == "text"
    assert any(f.event == "attachment" for f in frames)
    texts = [f.data["text"] for f in frames if f.event == "text"]
    assert "收到。" in texts


def test_remote_image_rejected(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    install_chat_mocks(monkeypatch)
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "img1", "images": ["https://127.0.0.1/x.png"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "bad_image"


def test_body_over_cap_is_413(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True, max_body_bytes=32))
    install_chat_mocks(monkeypatch)
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        content=b'{"text":"' + b"x" * 80 + b'","client_msg_id":"big"}',
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert r.json()["code"] == "payload_too_large"


def test_overflow_is_run_error(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True, queue_max=1))
    install_chat_mocks(monkeypatch)

    async def _flood(*, bot: CaptureBot, event: object, wall_clock: int, run_id: str) -> object:
        from gsuid_core.ai_core.handle_ai import PassiveChatResult

        await bot.send("one")
        await bot.send("two")
        return PassiveChatResult("ok")

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.bridge.run_http_agent_turn", _flood)
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "ov1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    frames = parse_sse_chunk(r.text)
    errors = [f for f in frames if f.event == "run.error"]
    assert len(errors) == 1
    assert errors[0].data["code"] == "output_truncated"


def test_group_id_accepted(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    install_chat_mocks(monkeypatch)
    captured: list[tuple[str, str | None]] = []

    async def _turn(*, bot: CaptureBot, event: object, wall_clock: int, run_id: str) -> object:
        from gsuid_core.models import Event
        from gsuid_core.ai_core.handle_ai import PassiveChatResult

        assert isinstance(event, Event)
        captured.append((event.user_type, event.group_id))
        await bot.send("ok")
        return PassiveChatResult("ok")

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.bridge.run_http_agent_turn", _turn)
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "g1", "group_id": "room_1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert captured == [("group", "room_1")]


def test_group_id_omitted_or_null_is_private(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    install_chat_mocks(monkeypatch)
    captured: list[tuple[str, str | None]] = []

    async def _turn(*, bot: CaptureBot, event: object, wall_clock: int, run_id: str) -> object:
        from gsuid_core.models import Event
        from gsuid_core.ai_core.handle_ai import PassiveChatResult

        assert isinstance(event, Event)
        captured.append((event.user_type, event.group_id))
        await bot.send("ok")
        return PassiveChatResult("ok")

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.bridge.run_http_agent_turn", _turn)
    client = make_client()
    headers = {"Authorization": f"Bearer {token}"}
    r1 = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "p1"},
        headers=headers,
    )
    r2 = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "p2", "group_id": None},
        headers=headers,
    )
    r3 = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "p3", "group_id": ""},
        headers=headers,
    )
    assert r1.status_code == r2.status_code == r3.status_code == 200
    assert captured == [("direct", None), ("direct", None), ("direct", None)]


def test_bad_group_id_400(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "gbad", "group_id": "a:b"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "bad_group"


def test_v1_event_names_only() -> None:
    from gsuid_core.ai_core.http_agent.types import SseEventName

    names: list[SseEventName] = ["run.start", "text", "attachment", "run.done", "run.error"]
    for name in names:
        chunk = encode_sse(name, {"seq": 1}, 1)
        assert f"event: {name}" in chunk
