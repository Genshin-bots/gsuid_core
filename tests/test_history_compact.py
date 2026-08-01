"""历史装配紧凑化回归：时间含秒、无双重 [历史对话]、默认不含当前轮。"""

from __future__ import annotations

import time

from gsuid_core.message_history import MessageRecord
from gsuid_core.ai_core.history_format import _format_timestamp, format_history_for_agent


def _rec(uid: str, name: str, content: str, ts: float) -> MessageRecord:
    return MessageRecord(role="user", user_id=uid, user_name=name, content=content, timestamp=ts)


def test_timestamp_includes_seconds() -> None:
    ts = time.time()
    s = _format_timestamp(ts)
    # 今天应为 HH:MM:SS
    assert s.count(":") == 2, s
    parts = s.split(":")
    assert all(len(p) == 2 for p in parts), s


def test_compact_single_line_and_one_header() -> None:
    t0 = time.time() - 120
    history = [
        _rec("1", "甲", "你好", t0),
        MessageRecord(role="assistant", user_id="bot", content="唔", timestamp=t0 + 5),
        _rec("2", "乙", "天气怎么样", t0 + 30),
    ]
    text = format_history_for_agent(history)
    assert text.count("[历史对话]") == 1
    assert "当前·" not in text
    # 说话人与本轮统一：名(用户ID:id)；时间在前
    assert "甲(用户ID:1)" in text and "你好" in text
    assert "] AI:" in text and "唔" in text
    # 不再用多行引号块
    assert '\n"你好"\n' not in text


def test_include_current_turn_optional() -> None:
    t0 = time.time() - 60
    history = [
        _rec("9", "主", "上一句", t0),
        _rec("9", "主", "当前句", t0 + 10),
    ]
    plain = format_history_for_agent(history, current_user_id="9", include_current_turn=False)
    assert "当前·" not in plain
    with_cur = format_history_for_agent(history, current_user_id="9", current_user_name="主", include_current_turn=True)
    assert "当前·主(用户ID:9)" in with_cur
    # 当前句不进历史块重复
    assert with_cur.count("当前句") == 1
