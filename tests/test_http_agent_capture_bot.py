"""CaptureBot：Bot 类型；无 send_bytes；主人 DM 不入队；出站 observe；omitted。"""

from __future__ import annotations

import base64
import asyncio

from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.http_agent.config import ATTACHMENT_FRAME_MAX
from gsuid_core.ai_core.http_agent.capture_bot import CaptureBot, CaptureItem


def _bot_pair() -> tuple[CaptureBot, asyncio.Queue[CaptureItem]]:
    q: asyncio.Queue[CaptureItem] = asyncio.Queue()
    ev = Event(bot_id="bot", bot_self_id="self", user_type="direct", user_id="u1", WS_BOT_ID="HTTP_AGENT")
    cap = CaptureBot(_Bot("HTTP_AGENT"), ev, q)
    return cap, q


def test_is_high_level_bot() -> None:
    cap, _q = _bot_pair()
    assert isinstance(cap, Bot)
    assert not hasattr(cap, "send_bytes")


def test_master_dm_via_target_send_not_queued() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        await cap.target_send("master-report", "direct", target_id="master-1")
        assert q.empty()
        await cap.send("visible")
        item = q.get_nowait()
        assert item.kind == "text"
        assert item.text == "visible"

    asyncio.run(_run())


def test_outbound_observe_called(monkeypatch) -> None:
    seen: list[str] = []

    async def _obs(**kwargs: object) -> None:
        content = kwargs["content"] if "content" in kwargs else ""
        seen.append(str(content))

    class _Mem:
        memory_mode = ["主动会话"]
        observer_blacklist: list[str] = []

    class _Flag:
        data = True

        def get_config(self, _k: str) -> _Flag:
            return self

    monkeypatch.setattr("gsuid_core.ai_core.memory.observer.observe", _obs)
    monkeypatch.setattr("gsuid_core.ai_core.memory.config.memory_config", _Mem())
    monkeypatch.setattr("gsuid_core.ai_core.configs.ai_config.ai_config", _Flag())
    cap, _q = _bot_pair()

    async def _run() -> None:
        await cap.send("hello-observe")

    asyncio.run(_run())
    assert seen == ["hello-observe"]


def test_receive_resp_fails_fast() -> None:
    cap, q = _bot_pair()

    async def _run() -> None:
        try:
            await cap.receive_resp("choose")
        except RuntimeError as e:
            assert "interactive wait" in str(e)
        else:
            raise AssertionError("expected RuntimeError")
        item = q.get_nowait()
        assert item.kind == "text"
        assert item.text == "choose"

    asyncio.run(_run())


def test_queue_overflow_sets_flag() -> None:
    q: asyncio.Queue[CaptureItem] = asyncio.Queue(maxsize=1)
    ev = Event(bot_id="bot", bot_self_id="self", user_type="direct", user_id="u1", WS_BOT_ID="HTTP_AGENT")
    cap = CaptureBot(_Bot("HTTP_AGENT"), ev, q)

    async def _run() -> None:
        await cap.send("one")
        await cap.send("two")
        assert cap._overflow is True
        assert q.qsize() == 1

    asyncio.run(_run())


def test_large_attachment_omitted() -> None:
    cap, q = _bot_pair()
    raw = b"x" * (ATTACHMENT_FRAME_MAX + 10)
    b64 = base64.b64encode(raw).decode("ascii")

    async def _run() -> None:
        await cap.send("base64://" + b64)
        item = q.get_nowait()
        assert item.kind == "attachment"
        assert item.encoding == "omitted"
        assert item.data == ""
        assert item.nbytes > ATTACHMENT_FRAME_MAX

    asyncio.run(_run())
