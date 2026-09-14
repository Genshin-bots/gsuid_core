"""巡检话头必须引用人类原句 + 近窗主动去重。"""

from gsuid_core.ai_core.heartbeat.decision import hook_cites_human_span
from gsuid_core.ai_core.heartbeat.dispatcher import (
    UnifiedProactiveDispatcher,
    texts_too_similar,
)


def test_hook_cites_human_span_requires_overlap() -> None:
    bodies = ["帮我看看平安银行今天怎么走", "这波放量有点意思"]
    assert hook_cites_human_span("平安银行这波放量我瞄了一眼", bodies)
    assert not hook_cites_human_span("三点多了还不睡，该找地方躺了", bodies)
    assert hook_cites_human_span("还不睡吗", ["还不睡吗"])
    assert not hook_cites_human_span("", bodies)


def test_proactive_texts_too_similar() -> None:
    a = "呼…五点了…群里还有人醒着…好困…"
    b = "呼…五点多了…群里还有人醒着…好困…"
    c = "平安银行放量了，我瞄了一眼绿的"
    assert texts_too_similar(a, b)
    assert not texts_too_similar(a, c)


def test_dispatcher_repeat_window() -> None:
    d = UnifiedProactiveDispatcher()
    key = "g1"
    first = "呼…五点了…群里还有人醒着…好困…"
    assert not d.would_repeat_heartbeat(key, first)
    d.remember_heartbeat_text(key, first)
    assert d.would_repeat_heartbeat(key, "呼…五点多了…群里还有人醒着…好困…")
    assert not d.would_repeat_heartbeat(key, "放量那一下我看了，绿的")


def test_dispatcher_repeat_window_write_cap_matches_read() -> None:
    d = UnifiedProactiveDispatcher()
    key = "g12"
    first = "第一条独特句子甲乙丙丁戊己庚辛"
    d.remember_heartbeat_text(key, first, window=12)
    for i in range(8):
        d.remember_heartbeat_text(key, f"填充句{i}一二三四五六七八九十", window=12)
    assert d.would_repeat_heartbeat(key, first, window=12)
    d8 = UnifiedProactiveDispatcher()
    d8.remember_heartbeat_text(key, first, window=8)
    for i in range(8):
        d8.remember_heartbeat_text(key, f"填充句{i}一二三四五六七八九十", window=8)
    assert not d8.would_repeat_heartbeat(key, first, window=8)


def test_peek_merge_survives_until_consume() -> None:
    d = UnifiedProactiveDispatcher()
    d.register_send("g1", "task", "平安银行按计划平仓了")
    assert "平仓" in d.peek_merge_context("g1")
    assert "平仓" in d.peek_merge_context("g1")
    assert "平仓" in d.consume_merge_context("g1")
    assert d.peek_merge_context("g1") == ""


def test_hook_cites_merge_context() -> None:
    human = ["今晚早点睡"]
    extra = "平安银行按计划平仓了，收益一点二"
    assert not hook_cites_human_span("平安银行平仓了", human)
    assert hook_cites_human_span("平安银行平仓了", [*human, extra])
