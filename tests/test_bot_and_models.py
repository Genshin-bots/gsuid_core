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
    monkeypatch.setattr("gsuid_core.ai_core.control.outbound_detectors.is_ooc_firewall_enabled", lambda: False)
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

    # 首气泡打字停顿（sleep）期间若有新用户消息插队，唤醒后自动补挂 Message("reply", msg_id)
    from gsuid_core.message_history import get_history_manager
    from gsuid_core.ai_core.inbox.turn import InboxTurn, bind_turn, reset_turn
    from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry

    hm = get_history_manager()
    ev_u1 = Event("OneBot", "900000001", "mid_1", "group", "group_pause_1", "100000001", {"nickname": "测试用户A"}, 6)
    ev_u2 = Event("OneBot", "900000001", "mid_2", "group", "group_pause_1", "100000002", {"nickname": "测试用户B"}, 6)
    hm.clear_history(ev_u1)
    try:
        rec1 = hm.add_message(ev_u1, "user", "测试提问", user_name="测试用户A", metadata={"msg_id": "mid_1"})
        assert rec1.link is not None
        turn = InboxTurn(
            session_id=ev_u1.session_id,
            persona_name="测试人格",
            entries=(
                PendingEntry(
                    WakeItem(rec1.link.seq, "100000001", "name", "hard", 1.0),
                    ev_u1,
                    persona_name="测试人格",
                ),
            ),
            from_seq=0,
            head_seq=rec1.link.seq,
        )
        captured: list[Union[Message, List[Message], str, bytes, List[str]]] = []
        interjected = [False]

        async def _interject_on_sleep(_sec: float) -> None:
            if not interjected[0]:
                interjected[0] = True
                hm.add_message(ev_u2, "user", "测试插话", user_name="测试用户B", metadata={"msg_id": "mid_2"})

        async def _capture_send(
            self: Bot,
            message: Union[Message, List[Message], str, bytes, List[str]],
            at_sender: bool = False,
            extra_metadata: Optional[Mapping[str, object]] = None,
            wait_recall: bool = False,
        ) -> None:
            captured.append(message)

        monkeypatch.setattr(ai_utils.asyncio, "sleep", _interject_on_sleep)
        monkeypatch.setattr("gsuid_core.ai_core.control.outbound_detectors.is_ooc_firewall_enabled", lambda: False)
        monkeypatch.setattr(Bot, "send", _capture_send)
        monkeypatch.setattr(ai_utils, "_persona_max_bubbles", lambda _ev: 2)
        tok = bind_turn(turn)
        try:
            asyncio.run(
                ai_utils.send_chat_result(
                    Bot(_Bot("ws_p1"), ev_u1),
                    f"<reply #{rec1.link.seq}>\n第一段回应。\n\n第二段回应。",
                    ev=ev_u1,
                    ooc_check=False,
                )
            )
        finally:
            reset_turn(tok)
        assert len(captured) == 2 and isinstance(captured[0], list) and isinstance(captured[1], list)
        seg0 = [(s.type, s.data) for s in captured[0] if isinstance(s, Message)]
        seg1 = [(s.type, s.data) for s in captured[1] if isinstance(s, Message)]
        assert seg0[0] == ("reply", "mid_1")
        assert all(t != "reply" for t, _ in seg1)
    finally:
        hm.clear_history(ev_u1)


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

    # 引用对象出窗时，QQ 随引用自动附带的 quote_auto_at 必须落为 quote_self 而非伪造 at_self
    auto_rec = mgr.add_message(
        b_ev,
        "user",
        "这",
        user_name="测试用户B",
        metadata={"msg_id": "q_auto", "at_bot": True, "quote_auto_at": True, "reply_id": "lost_proactive"},
    )
    assert auto_rec.link is not None
    assert auto_rec.link.reply_to_author == "self"
    assert auto_rec.link.at_self is False

    # 出站缺失 bot_self_id 时从活跃会话反查真实身份；echo 延迟回写 msg_id 后清掉负缓存
    res_bid, res_self = mgr.resolve_session_identity(
        ws_bot_id=None, bot_id="Bot", target_type="group", target_id="70001"
    )
    assert (res_bid, res_self) == ("OneBot", "900000001")
    delayed = mgr.add_message(bot_ev, "assistant", "主动播报", user_name="AI", metadata={})
    assert mgr.find_by_msg_id(b_ev, "late_mid_1") is None
    mgr.attach_msg_id(delayed, "late_mid_1")
    assert mgr.find_by_msg_id(b_ev, "late_mid_1") is delayed
    assert delayed.link is not None and delayed.link.msg_id == "late_mid_1"


def test_outbound_bubble_packing_and_detached_turn_scope() -> None:
    """超限分段无损合入末条气泡，detached_turn_scope 隔离前台轮次上下文。"""
    from gsuid_core.ai_core.utils import pack_outbound_bubbles
    from gsuid_core.ai_core.inbox.turn import InboxTurn, bind_turn, reset_turn, current_turn, detached_turn_scope

    packed = pack_outbound_bubbles(["第一段", "第二段", "第三段", "第四段"], 2)
    assert packed == ["第一段", "第二段\n\n第三段\n\n第四段"]
    assert pack_outbound_bubbles(["单段"], 0) == []
    assert pack_outbound_bubbles(["甲", "乙"], 4) == ["甲", "乙"]

    turn = InboxTurn(session_id="group:200000001", persona_name="测试角色", entries=(), from_seq=0, head_seq=1)
    tok = bind_turn(turn)
    try:
        assert current_turn() is turn
        with detached_turn_scope():
            assert current_turn() is None
        assert current_turn() is turn
    finally:
        reset_turn(tok)


def test_convert_message_link_image_to_base64_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """link:// 图片按 base64 模式转换时，首次下载与缓存命中均须带 base64:// 前缀。"""
    import base64

    from gsuid_core import segment

    raw_bytes = b"\x89PNG\r\n\x1a\ntest-image"
    expected = f"base64://{base64.b64encode(raw_bytes).decode('utf-8')}"
    calls: List[str] = []

    class _FakeResp:
        content: bytes = raw_bytes

    class _FakeCfg:
        data: str = "base64"

    class _FakeSendPicConfig:
        def get_config(self, _bot_id: str, _default: str = "base64") -> _FakeCfg:
            return _FakeCfg()

    async def _fake_sget(url: str) -> _FakeResp:
        calls.append(url)
        return _FakeResp()

    async def _fake_global_val(_bot_id: str, _bot_self_id: str) -> dict[str, int]:
        return {"image": 0}

    monkeypatch.setattr(segment, "URL_MAP", {})
    monkeypatch.setattr(segment, "sget", _fake_sget)
    monkeypatch.setattr(segment, "get_global_val", _fake_global_val)
    monkeypatch.setattr(segment, "send_pic_config", _FakeSendPicConfig())

    msg = Message(type="image", data="link://https://example.invalid/sample.png")
    first = asyncio.run(segment.convert_message(msg, "onebot", "self1"))
    second = asyncio.run(segment.convert_message(msg, "onebot", "self1"))

    assert first == [Message(type="image", data=expected)]
    assert second == [Message(type="image", data=expected)]
    assert calls == ["https://example.invalid/sample.png"]
