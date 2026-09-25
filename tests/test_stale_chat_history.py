"""12 小时库存与 read_chat_history：按词/时间点取发言，不一次倒出。"""

from __future__ import annotations

import time

from gsuid_core.models import Event
from gsuid_core.message_history.manager import MessageRecord, HistoryManager
from gsuid_core.ai_core.buildin_tools.chat_history import read_chat_history_text


def _ev(user_id: str, group_id: str | None, user_type: str = "group") -> Event:
    return Event(
        bot_id="onebot",
        bot_self_id="3399",
        user_id=user_id,
        group_id=group_id,
        user_type="direct" if user_type == "direct" else "group",
        WS_BOT_ID="NoneBot2",
    )


def test_history_keeps_rolling_twelve_hours() -> None:
    mgr = HistoryManager()
    ev = _ev("321", "929")
    now = time.time()
    storage = None
    mgr.add_message(ev, "user", "锚点", user_name="甲")
    storage = mgr._get_storage_event(ev)
    mgr._histories[storage].append(
        MessageRecord(role="user", content="十一小时前还在", user_id="321", timestamp=now - 11 * 3600)
    )
    mgr._histories[storage].appendleft(
        MessageRecord(role="user", content="十三小时前太早", user_id="321", timestamp=now - 13 * 3600)
    )
    mgr.add_message(ev, "user", "刚刚", user_name="甲")
    texts = [rec.content for rec in mgr.get_history(ev)]
    assert "十三小时前太早" not in texts
    assert "十一小时前还在" in texts
    assert "刚刚" in texts
    bare = read_chat_history_text(mgr, ev, "", is_master=False)
    assert "刚刚" not in bare
    assert "最近12小时" in bare
    found = read_chat_history_text(mgr, ev, "", query="十一小时", is_master=False)
    assert "十一小时前还在" in found
    assert "刚刚" not in found
    stamp = time.strftime("%H:%M", time.localtime(now - 11 * 3600))
    around = read_chat_history_text(mgr, ev, "", at=stamp, radius_minutes=10, is_master=False, now=now)
    assert "十一小时前还在" in around
    assert "刚刚" not in around


def test_read_other_group_and_block_foreign_private() -> None:
    mgr = HistoryManager()
    here = _ev("321", "929")
    other = _ev("1", "666")
    private = _ev("888", None, "direct")
    own_private = _ev("321", None, "direct")
    mgr.add_message(here, "user", "本群刚才说了某主播", user_name="甲")
    mgr.add_message(other, "user", "另一群的手办", user_name="乙")
    mgr.add_message(private, "user", "私聊秘密", user_name="丙")
    mgr.add_message(own_private, "user", "自己的私聊", user_name="甲")

    current = read_chat_history_text(mgr, here, "", query="某主播", is_master=False)
    assert "某主播" in current
    assert "手办" not in current

    listed = read_chat_history_text(mgr, here, "list", is_master=False)
    assert "group:666" in listed
    assert "private:321" in listed
    assert "private:888" not in listed

    other_text = read_chat_history_text(mgr, here, "group:666", query="手办", is_master=False)
    assert "另一群的手办" in other_text
    assert "不含图片内容" in other_text

    denied = read_chat_history_text(mgr, here, "private:888", is_master=False)
    assert denied == "别人的私聊记录不能读。"
    allowed = read_chat_history_text(mgr, here, "private:888", query="秘密", is_master=True)
    assert "私聊秘密" in allowed
    own = read_chat_history_text(mgr, here, "private:321", query="自己", is_master=False)
    assert "自己的私聊" in own


def test_read_chat_history_keeps_match_in_long_paste() -> None:
    mgr = HistoryManager()
    ev = _ev("321", "929")
    needle = "某主播同期还有谁"
    long = ("前" * 5000) + needle + ("后" * 100)
    mgr.add_message(ev, "user", long, user_name="甲")
    found = read_chat_history_text(mgr, ev, "", query=needle, is_master=False)
    assert needle in found
    stats = mgr.get_stats()
    assert "max_messages_per_session" not in stats
    assert stats["total_messages"] == 1
