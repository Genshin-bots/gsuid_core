"""走共用被动入口；不 handle_event；不占 _ai_semaphore；H00；纯图 A 轨；预算 fail-open；limiter finally。"""

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

from gsuid_core.models import Event
from gsuid_core.ai_core.handle_ai import PassiveChatResult
from gsuid_core.ai_core.http_agent.keys import reset_key_store_for_tests
from gsuid_core.ai_core.http_agent.bridge import make_capture_bot, run_http_agent_turn, attach_inbound_tracks
from gsuid_core.ai_core.http_agent.limiter import LimitExceeded, limiter
from gsuid_core.ai_core.http_agent.runtime import ActiveRun, get_run, register_run
from gsuid_core.ai_core.http_agent.capture_bot import CaptureItem


def setup_function() -> None:
    reset_http_agent_runtime()


def test_bridge_calls_passive_not_handle_event(monkeypatch) -> None:
    he_calls: list[int] = []
    passive_calls: list[int] = []

    async def _he(*_a: object, **_k: object) -> None:
        he_calls.append(1)

    async def _passive(*_a: object, **_k: object) -> PassiveChatResult:
        passive_calls.append(1)
        return PassiveChatResult("silence")

    class _Agent:
        _cancel_generation = asyncio.Event()

    async def _sess(_event: object) -> _Agent:
        return _Agent()

    monkeypatch.setattr("gsuid_core.handler.handle_event", _he)
    monkeypatch.setattr("gsuid_core.ai_core.http_agent.bridge.run_passive_interactive_chat", _passive)
    monkeypatch.setattr("gsuid_core.ai_core.ai_router.get_ai_session", _sess)

    ev = Event(bot_id="bot", bot_self_id="k_default", user_type="direct", user_id="u1", WS_BOT_ID="HTTP_AGENT")
    ev.raw_text = "hi"

    async def _run() -> None:
        q: asyncio.Queue[CaptureItem] = asyncio.Queue()
        bot = make_capture_bot(ev, q)
        await run_http_agent_turn(bot=bot, event=ev, wall_clock=600, run_id="r1")

    asyncio.run(_run())
    assert passive_calls == [1]
    assert he_calls == []


def test_binds_agent_before_passive(monkeypatch) -> None:
    order: list[str] = []

    class _Agent:
        _cancel_generation = asyncio.Event()

    agent = _Agent()

    async def _sess(_event: object) -> _Agent:
        order.append("session")
        return agent

    async def _passive(*_a: object, **_k: object) -> PassiveChatResult:
        order.append("passive")
        return PassiveChatResult("silence")

    monkeypatch.setattr("gsuid_core.ai_core.ai_router.get_ai_session", _sess)
    monkeypatch.setattr("gsuid_core.ai_core.http_agent.bridge.run_passive_interactive_chat", _passive)
    ev = Event(bot_id="bot", bot_self_id="k_default", user_type="direct", user_id="u1", WS_BOT_ID="HTTP_AGENT")
    ev.raw_text = "hi"

    async def _run() -> None:
        q: asyncio.Queue[CaptureItem] = asyncio.Queue()
        bot = make_capture_bot(ev, q)
        dummy = asyncio.create_task(asyncio.sleep(60))
        register_run(ActiveRun(run_id="r-bind", key_id="k", agent_session_id=ev.session_id, turn_task=dummy))
        await run_http_agent_turn(bot=bot, event=ev, wall_clock=600, run_id="r-bind")
        dummy.cancel()
        try:
            await dummy
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())
    assert order == ["session", "passive"]
    live = get_run("r-bind")
    assert live is not None
    assert live.agent is agent


def test_h00_inbound_hook(monkeypatch) -> None:
    fired: list[object] = []

    async def _fire(point: object, _ctx: object) -> None:
        fired.append(point)

    class _Hist:
        def add_message(self, event: Event, role: str, content: str, **_k: object) -> None:
            return None

    monkeypatch.setattr("gsuid_core.message_history.get_history_manager", lambda: _Hist())
    monkeypatch.setattr("gsuid_core.ai_core.hooks.should_fire", lambda _p: True)
    monkeypatch.setattr("gsuid_core.ai_core.hooks.fire_hooks", _fire)
    ev = Event(bot_id="bot", bot_self_id="k_default", user_type="direct", user_id="u1", WS_BOT_ID="HTTP_AGENT")
    ev.raw_text = "hi"

    async def _run() -> None:
        q: asyncio.Queue[CaptureItem] = asyncio.Queue()
        bot = make_capture_bot(ev, q)
        await attach_inbound_tracks(bot, ev)
        await asyncio.sleep(0)

    asyncio.run(_run())
    assert fired


def test_inbound_records_image_only(monkeypatch) -> None:
    added: list[tuple[str, str]] = []

    class _Hist:
        def add_message(self, event: Event, role: str, content: str, **_k: object) -> None:
            added.append((role, content))

    monkeypatch.setattr("gsuid_core.message_history.get_history_manager", lambda: _Hist())
    monkeypatch.setattr("gsuid_core.ai_core.hooks.should_fire", lambda _p: False)

    ev = Event(bot_id="bot", bot_self_id="k_default", user_type="direct", user_id="u1", WS_BOT_ID="HTTP_AGENT")
    ev.raw_text = ""
    ev.image_id_list = ["img1"]

    async def _run() -> None:
        q: asyncio.Queue[CaptureItem] = asyncio.Queue()
        bot = make_capture_bot(ev, q)
        await attach_inbound_tracks(bot, ev)

    asyncio.run(_run())
    assert added == [("user", "")]


def test_evaluate_budget_fail_open(monkeypatch) -> None:
    from gsuid_core.ai_core.turn_pipeline import evaluate_budget

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("budget backend down")

    monkeypatch.setattr("gsuid_core.ai_core.budget.manager.budget_manager.check_scope", _boom)
    ev = Event(bot_id="bot", user_type="direct", user_id="u1")

    async def _run() -> None:
        decision = await evaluate_budget(ev)
        assert decision is None

    asyncio.run(_run())


def test_limiter_finally_releases(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True, max_concurrent=1, per_key_concurrent=1))
    install_chat_mocks(monkeypatch)

    async def _boom(*, bot: object, event: object, wall_clock: int, run_id: str) -> PassiveChatResult:
        raise RuntimeError("turn crashed")

    monkeypatch.setattr("gsuid_core.ai_core.http_agent.bridge.run_http_agent_turn", _boom)
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "boom"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    used, _per = limiter.snapshot()
    assert used == 0
    try:
        asyncio.run(limiter.try_acquire(rec["key_id"]))
    except LimitExceeded as e:
        raise AssertionError(f"slot leaked: {e}") from e
    asyncio.run(limiter.release(rec["key_id"]))


def test_does_not_use_ai_semaphore(monkeypatch, tmp_path: Path) -> None:
    store = reset_key_store_for_tests(tmp_path / "keys.json")
    token, _rec = store.create(user_id="u1", bot_id="bot")
    patch_settings(monkeypatch, sample_settings(enable=True))
    install_chat_mocks(monkeypatch)
    acquires: list[int] = []

    class _Sem:
        async def acquire(self) -> None:
            acquires.append(1)

        def release(self) -> None:
            return None

        def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr("gsuid_core.ai_core.handle_ai._ai_semaphore", _Sem())
    client = make_client()
    r = client.post(
        "/api/v1/agent/chat/stream",
        json={"text": "hi", "client_msg_id": "s1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert acquires == []
