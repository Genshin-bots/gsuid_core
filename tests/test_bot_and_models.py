"""Bot 构造、消息模型与气泡拆条测试。"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import List, Union, Mapping, Optional

import pytest

from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event
from gsuid_core.ai_core import utils as ai_utils
from gsuid_core.segment import Message
from gsuid_core.ai_core.trigger_bridge import MockBot
from gsuid_core.ai_core.persona.chat_style import DEFAULT_CHAT_STYLE, _tier_for


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


def test_high_level_bot_wraps_low_level() -> None:
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
    assert high.bot is low
    assert high.ev is ev
    assert not hasattr(low, "ev")
    assert high.bot_id == ev.bot_id
    assert hasattr(high, "send")


def test_mockbot_requires_high_level_bot() -> None:
    low = _Bot("ws-alpha")
    ev = Event(bot_id="onebot", bot_self_id="self1", user_type="direct", user_id="u1", WS_BOT_ID="ws-alpha")
    high = Bot(low, ev)
    mock = MockBot(high, {"image_ids": []})
    assert object.__getattribute__(mock, "_real_bot") is high


def test_blank_line_survives_leak_stripper() -> None:
    assert ai_utils.strip_framework_user_leaks("第一句。\n\n第二句。") == "第一句。\n\n第二句。"


def test_send_chat_result_splits_on_blank_line(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run("第一句\n\n第二句", monkeypatch)
    assert len(out) == 2
    assert out[0].strip() == "第一句"
    assert out[1].strip() == "第二句"


def test_send_chat_result_clamps_to_persona_bubble_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _run("一\n\n二\n\n三\n\n四", monkeypatch, bubbles=2)
    assert len(out) == 2
    assert out[0].strip() == "一"
    assert "二" in out[1] and "四" in out[1]


def test_send_chat_result_turns_cq_at_into_at_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = _drive("[CQ:at,qq=169629556,name=foo] 到时间啦", monkeypatch)
    assert len(sent) == 1
    assert isinstance(sent[0], list)
    pairs = [(seg.type, seg.data) for seg in sent[0] if isinstance(seg, Message)]
    assert pairs == [("at", "169629556"), ("text", " 到时间啦")]


def test_bubble_cap_follows_chat_style(monkeypatch: pytest.MonkeyPatch) -> None:
    src = "一\n\n二\n\n三\n\n四"
    assert len(_run(src, monkeypatch, bubbles=1)) == 1
    assert len(_run(src, monkeypatch, bubbles=4)) == 4


def test_chat_style_tier_boundaries() -> None:
    assert _tier_for(0)[0] == 1
    assert _tier_for(DEFAULT_CHAT_STYLE)[0] == 2
    assert _tier_for(100)[0] >= 3


def test_chat_style_hint_is_persona_neutral() -> None:
    banned = ("唔", "呼", "zzz", "早柚", "貉", "股票", "黄金", "原神", "提瓦特")
    for value in (0, 50, 100):
        hint = _tier_for(value)[3]
        assert hint
        assert not any(word in hint for word in banned)


def test_chat_style_dataclass_is_frozen() -> None:
    from gsuid_core.ai_core.persona.chat_style import ChatStyle

    style = ChatStyle(bubbles=2, soft=60, hard=150, segment_hint="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(style, "bubbles", 3)
