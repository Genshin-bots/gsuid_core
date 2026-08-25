"""Session ID 与群聊历史隔离：群不含 user_id；同群共享 deque。"""

from __future__ import annotations

from gsuid_core.models import Event
from gsuid_core.message_history.manager import HistoryManager
from gsuid_core.ai_core.session_registry import parse_session_scope


def _group_ev(user_id: str) -> Event:
    return Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g9001",
        user_id=user_id,
        WS_BOT_ID="ws1",
    )


def _private_ev(user_id: str) -> Event:
    return Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="direct",
        group_id=None,
        user_id=user_id,
        WS_BOT_ID="ws1",
    )


def test_group_session_id_omits_user_id() -> None:
    a = _group_ev("u1")
    b = _group_ev("u2")
    assert a.session_id == b.session_id
    assert a.session_id.endswith(":group:g9001")
    assert ":private:" not in a.session_id
    assert "u1" not in a.session_id
    assert "u2" not in b.session_id


def test_private_session_id_is_per_user() -> None:
    a = _private_ev("u1")
    b = _private_ev("u2")
    assert a.session_id != b.session_id
    assert a.session_id.endswith(":private:u1")
    assert b.session_id.endswith(":private:u2")
    assert ":group:" not in a.session_id


def test_parse_session_scope_roundtrip() -> None:
    bot, kind, sid = parse_session_scope(_group_ev("u1").session_id)
    assert (bot, kind, sid) == ("self1", "group", "g9001")
    bot2, kind2, sid2 = parse_session_scope(_private_ev("u9").session_id)
    assert (bot2, kind2, sid2) == ("self1", "private", "u9")
    assert parse_session_scope("odd") == ("", "", "")


def test_group_history_shared_private_isolated() -> None:
    hm = HistoryManager()
    g1 = _group_ev("u1")
    g2 = _group_ev("u2")
    hm.add_message(g1, "user", "from-u1")
    recs = hm.get_history(g2)
    assert len(recs) == 1
    assert recs[0].content == "from-u1"

    p1 = _private_ev("u1")
    p2 = _private_ev("u2")
    hm.add_message(p1, "user", "priv-u1")
    assert hm.get_history(p2) == []
    priv = hm.get_history(p1)
    assert len(priv) == 1
    assert priv[0].content == "priv-u1"


def test_storage_event_blanks_group_user_id() -> None:
    hm = HistoryManager()
    key = hm._get_storage_event(_group_ev("u1"))
    assert key.user_id == ""
    assert key.group_id == "g9001"
    priv = hm._get_storage_event(_private_ev("u1"))
    assert priv.user_id == "u1"
