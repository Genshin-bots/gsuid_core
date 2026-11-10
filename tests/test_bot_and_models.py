"""Bot 构造、消息模型与气泡拆条测试。"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import List, Union, Mapping, Optional

import pytest

from gsuid_core.bot import (
    Bot,
    BotReceiveMode,
    BotConnectionState,
    RecallCapabilityState,
    _Bot,
)
from gsuid_core.models import Event
from gsuid_core.ai_core import utils as ai_utils
from gsuid_core.segment import Message
from gsuid_core.ai_core.trigger_bridge import MockBot
from gsuid_core.ai_core.persona.chat_style import DEFAULT_CHAT_STYLE, ChatStyle, _tier_for


def _plain_text(message: Union[Message, List[Message], str, bytes, List[str]]) -> str:
    if isinstance(message, str):
        return message
    segs = message if isinstance(message, list) else [message]
    out = ""
    for seg in segs:
        if isinstance(seg, Message):
            data = seg.data
            out += data if isinstance(data, str) else ""
    return out


async def _no_sleep(_seconds: float) -> None:
    return None


def _drive(
    text: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bubbles: Optional[int] = None,
) -> List[Union[Message, List[Message], str, bytes, List[str]]]:
    sent: List[Union[Message, List[Message], str, bytes, List[str]]] = []

    async def _fake_send(
        self: Bot,
        message: Union[Message, List[Message], str, bytes, List[str]],
        at_sender: bool = False,
        extra_metadata: Optional[Mapping[str, object]] = None,
        wait_recall: bool = False,
    ) -> None:
        sent.append(message)

    monkeypatch.setattr(ai_utils.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr("gsuid_core.ai_core.output_firewall.is_enabled", lambda: False)
    monkeypatch.setattr(Bot, "send", _fake_send)
    if bubbles is not None:
        monkeypatch.setattr(ai_utils, "_persona_max_bubbles", lambda ev: bubbles)
    asyncio.run(ai_utils.send_chat_result(Bot.__new__(Bot), text, ev=None, ooc_check=False))
    return sent


def _run(text: str, monkeypatch: pytest.MonkeyPatch, *, bubbles: Optional[int] = None) -> List[str]:
    return [_plain_text(message) for message in _drive(text, monkeypatch, bubbles=bubbles)]


def test_bot_wrapper_and_chat_style_contract() -> None:
    low = _Bot("ws-alpha")
    ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g1",
        user_id="u1",
        WS_BOT_ID="ws-alpha",
    )
    high = Bot(low, ev)
    assert high.bot is low and high.ev is ev and not hasattr(low, "ev")
    assert high.bot_id == ev.bot_id and hasattr(high, "send")
    assert low.connection_state == BotConnectionState.EXPIRED
    assert low.recall_capability_state == RecallCapabilityState.PROBING
    low._disconnected_at = 1000.0
    assert low.get_connection_state(now=1100.0) == BotConnectionState.RETAINED
    assert low.get_connection_state(now=1400.0) == BotConnectionState.EXPIRED
    low._supports_recall = True
    assert low.recall_capability_state == RecallCapabilityState.SUPPORTED
    low._supports_recall = False
    assert low.recall_capability_state == RecallCapabilityState.UNSUPPORTED
    assert high.receive_mode == BotReceiveMode.IDLE
    high.receive_tag = True
    assert high.receive_mode == BotReceiveMode.SINGLE
    high.mutiply_tag = True
    assert high.receive_mode == BotReceiveMode.MULTI
    high.set_event()
    high.set_mutiply_event()
    assert high.event.is_set() and high.mutiply_event.is_set()

    mock = MockBot(high, {"image_ids": []})
    assert object.__getattribute__(mock, "_real_bot") is high
    assert ai_utils.strip_framework_user_leaks("第一句。\n\n第二句。") == "第一句。\n\n第二句。"

    assert _tier_for(0)[0] == 1 and _tier_for(DEFAULT_CHAT_STYLE)[0] == 2 and _tier_for(100)[0] >= 3
    banned = ("唔", "呼", "zzz", "早柚", "貉", "股票", "黄金", "原神", "提瓦特")
    for value in (0, 50, 100):
        hint = _tier_for(value)[3]
        assert hint and not any(word in hint for word in banned)

    style = ChatStyle(bubbles=2, soft=60, hard=150, segment_hint="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(style, "bubbles", 3)


def test_send_chat_result_splitting_and_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run("第一句\n\n第二句", monkeypatch)
    assert [s.strip() for s in out] == ["第一句", "第二句"]

    clamped = _run("一\n\n二\n\n三\n\n四", monkeypatch, bubbles=2)
    assert len(clamped) == 2 and clamped[0].strip() == "一" and "二" in clamped[1] and "四" in clamped[1]
    assert len(_run("一\n\n二\n\n三\n\n四", monkeypatch, bubbles=1)) == 1
    assert len(_run("一\n\n二\n\n三\n\n四", monkeypatch, bubbles=4)) == 4

    sent = _drive("[CQ:at,qq=100000002,name=foo] 到时间啦", monkeypatch)
    assert len(sent) == 1 and isinstance(sent[0], list)
    pairs = [(seg.type, seg.data) for seg in sent[0] if isinstance(seg, Message)]
    assert pairs == [("at", "100000002"), ("text", " 到时间啦")]
