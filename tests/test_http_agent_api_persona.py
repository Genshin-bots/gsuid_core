"""人格 422/409；冲突路径不写 override。"""

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
from gsuid_core.ai_core.http_agent.persona import PersonaResolveError, resolve_http_persona


def setup_function() -> None:
    reset_http_agent_runtime()


def _key(persona: str = "") -> HttpAgentKeyRecord:
    return HttpAgentKeyRecord(
        key_id="abcd1234",
        token_hash="h",
        user_id="u1",
        bot_id="bot",
        user_pm=6,
        persona=persona,
        label="",
        created_at=0.0,
        revoked=False,
    )


def test_persona_unbound(monkeypatch) -> None:
    monkeypatch.setattr("gsuid_core.ai_core.http_agent.persona.peek_bound_persona", lambda _sid: None)
    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.config.persona_config_manager.get_persona_for_session",
        lambda _sid: None,
        raising=False,
    )
    try:
        resolve_http_persona(session_id="HTTP_AGENT:bot:abcd1234_default:private:u1", key=_key(), requested=None)
        raise AssertionError("expected PersonaResolveError")
    except PersonaResolveError as e:
        assert e.status == 422
        assert e.code == "persona_unbound"


def test_persona_pinned_does_not_write_override(monkeypatch) -> None:
    writes: list[tuple[str, str]] = []

    def _set(sid: str, name: str) -> None:
        writes.append((sid, name))

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.persona.peek_bound_persona", lambda _sid: "Alice")
    monkeypatch.setattr(
        "gsuid_core.buildin_plugins.core_command.core_ai_control.state.set_persona_override",
        _set,
        raising=False,
    )
    try:
        resolve_http_persona(
            session_id="HTTP_AGENT:bot:abcd1234_default:private:u1",
            key=_key(),
            requested="Bob",
        )
        raise AssertionError("expected PersonaResolveError")
    except PersonaResolveError as e:
        assert e.status == 409
        assert e.code == "persona_pinned"
    assert writes == []


def test_stream_maps_persona_unbound(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    install_chat_mocks(monkeypatch)

    def _boom(**_k: object) -> str:
        raise PersonaResolveError(422, "persona_unbound", "no persona")

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.persona.resolve_http_persona", _boom)
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "m1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 422
    assert r.json()["code"] == "persona_unbound"
