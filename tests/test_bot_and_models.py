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


def test_event_link_seq_reply_and_at_self() -> None:
    """EventLink：seq 单调递增、引用边解析到 uid/self/None、自动@不置 at_self、dict 往返保 link。"""
    from gsuid_core.message_history import MessageRecord, HistoryManager

    mgr = HistoryManager()

    def ev(uid: str, nick: str) -> Event:
        return Event("OneBot", "900000001", "", "group", "70001", uid, {"nickname": nick}, 6)

    c_ev = ev("100000003", "测试用户C")
    b_ev = ev("100000002", "测试用户B")
    bot_ev = ev("900000001", "测试人格")

    u1 = mgr.add_message(c_ev, "user", "让你画图", user_name="测试用户C", metadata={"msg_id": "m1"})
    bot1 = mgr.add_message(
        bot_ev,
        "assistant",
        "唔…好",
        user_name="测试人格",
        metadata={"msg_id": "b1", "speech_channel": "persona"},
    )
    assert u1.link is not None and bot1.link is not None and u1.link.seq < bot1.link.seq

    cases = [
        # (reply_id, at_bot, 期望 reply_to_author, 期望 reply_to_seq, 期望 at_self)
        ("m1", False, "100000003", u1.link.seq, False),  # 用户引用用户
        ("b1", True, "self", bot1.link.seq, False),  # 引用 Bot 自带自动@不算 at_self
        ("b1", False, "self", bot1.link.seq, False),  # 用户引用 Bot
        ("lost", True, None, None, True),  # 引用对象已出窗口：无引用边，真@仍算
        (None, True, None, None, True),  # 纯 @
        (None, False, None, None, False),
    ]
    last_seq = bot1.link.seq
    for i, (reply_id, at_bot, author, rseq, aself) in enumerate(cases):
        meta: dict[str, object] = {"msg_id": f"q{i}", "at_bot": at_bot}
        if reply_id is not None:
            meta["reply_id"] = reply_id
        rec = mgr.add_message(b_ev, "user", f"case{i}", user_name="测试用户B", metadata=meta)
        link = rec.link
        assert link is not None
        assert link.reply_to_author == author, f"case{i}"
        assert link.reply_to_seq == rseq, f"case{i}"
        assert link.at_self is aself, f"case{i}"
        assert link.seq > last_seq
        last_seq = link.seq
        rt = MessageRecord.from_dict(rec.to_dict())
        assert rt.link == link

    assert mgr.head_seq(b_ev) == last_seq
    after = mgr.records_after(b_ev, bot1.link.seq)
    assert [r.link.seq for r in after if r.link] == list(range(bot1.link.seq + 1, last_seq + 1))
    found = mgr.find_by_seq(b_ev, u1.link.seq)
    assert found is not None and found.content == "让你画图"
