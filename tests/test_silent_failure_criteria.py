"""这批锁的都是「判据写错了会静默丢能力」的地方，不是形状断言。

- 临时词枚举用 \\b 圈汉字：Python re 的 \\w 含 CJK，中文显式日期窗整条失效。
- 探针重试被 status_code==200 短路：拒答裹在 200 里时退避重试从未生效。
- 能力节点关键词收录泛用词：子串最长优先命中后，联网意图被抽掉。
- 内联等回终态后不 claim：与执行体的投递撞车，用户收两条「任务完成」。
"""

from __future__ import annotations

from datetime import datetime

# ── 临时词枚举的边界：\\b 只对 ASCII 成立 ──────────────────────────────


def test_temporal_enum_matches_chinese_inside_a_sentence() -> None:
    """汉字之间没有 \\b 边界，中文枚举词必须不带边界匹配。

    这条是 query_explicit_time_range 的闸门：匹配不到就不会收窄时间窗。
    """
    from gsuid_core.ai_core.memory.retrieval.event_time import query_explicit_time_range

    q = "请按时间顺序总结 2024 年 1 月 1 日到 2024 年 6 月 1 日发生了什么"
    got = query_explicit_time_range(q)
    assert got is not None, "中文显式日期窗必须被识别"
    assert got[0] == datetime(2024, 1, 1)
    assert got[1] == datetime(2024, 6, 2), "上界是当天结束（含当日）"


def test_temporal_enum_still_respects_english_word_boundary() -> None:
    """英文那半边不能退回无边界：裸 summar 会把 summary 砍成孤立字母 y。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import _TEMPORAL_ENUM_RE

    assert _TEMPORAL_ENUM_RE.search("give me a summary of the work"), "整词必须命中"
    assert _TEMPORAL_ENUM_RE.search("list them in order")
    # 关键：summary 整词命中后，剥枚举词不该把词砍碎
    stripped = _TEMPORAL_ENUM_RE.sub("", "summary")
    assert stripped.strip() != "y", f"summary 被砍成了 {stripped!r}"


def test_explicit_date_window_allows_spaces_around_cjk_units() -> None:
    """中文正文常写「2024 年 1 月 1 日」；不容忍空格时整句匹配不到。

    与枚举词那道是**两个独立的门**：只修 \\b 不动这里，带空格的句子照样不收窄。
    """
    from gsuid_core.ai_core.memory.retrieval.event_time import query_explicit_time_range

    spaced = query_explicit_time_range("请按时间顺序总结 2024 年 1 月 1 日到 2024 年 6 月 1 日发生了什么")
    tight = query_explicit_time_range("请按时间顺序总结 2024年1月1日到 2024年6月1日发生了什么")
    assert spaced is not None and tight is not None
    assert spaced == tight, "带空格与不带空格必须解析成同一个窗口"


def test_point_query_without_temporal_word_does_not_narrow() -> None:
    """点查不该触发时间窗收窄（闸门仍在）。"""
    from gsuid_core.ai_core.memory.retrieval.event_time import query_explicit_time_range

    assert query_explicit_time_range("2024 年 1 月 1 日那天我在干什么") is None


# ── 探针重试：200 包着拒答也必须退避 ──────────────────────────────────


def test_retryable_treats_http_200_refusal_as_retryable() -> None:
    """上游把拒答/配额错误裹在 200 里返回，按状态码判就永远不重试。"""
    from eval.common.beam_runner import _retryable

    assert _retryable(200, "稍等，这会儿不太方便，稍后再试。")
    assert _retryable(200, "执行出错: 模型套餐用量已达上限")
    assert _retryable(200, "I'm overloaded, try again later")
    assert _retryable(503, "anything"), "真 5xx 仍要重试"
    assert _retryable(-1, ""), "连不上也要重试"


def test_retryable_accepts_a_real_long_answer() -> None:
    """好答案不能被误判成拒答——退避重试是有代价的，不能滥用。"""
    from eval.common.beam_runner import _retryable

    good = "Here are the steps.\n" + ("Define each layer cleanly. " * 200) + "Nobody's overloaded."
    assert len(good) > 400
    assert not _retryable(200, good)
    assert not _retryable(200, "2024 年 3 月你先把 Redis 缓存接上，后来又补了限流。")


# ── 能力节点关键词：泛用词会剥夺网页检索 ──────────────────────────────


def test_generic_words_are_not_capability_match_keywords() -> None:
    """命中能力节点后主循环会禁止继续网页检索，误判代价不对称。"""
    from gsuid_core.ai_core.agent_node.registry import match_capability_node
    from gsuid_core.ai_core.capability_agents.profiles import register_builtin_profiles

    register_builtin_profiles()  # import 无副作用，节点靠这个函数注册
    # 这些词在正常对话里遍地都是，进 match_keywords 就会赢过真正的专有词
    for phrase in ("这些题我们都做过，网上对一下答案", "那些东西我都加过了"):
        node = match_capability_node(phrase)
        assert node != "internal_reporter", f"{phrase!r} 被路由到不查 web 的内部报告员"


def test_specific_words_still_route_to_internal_reporter() -> None:
    """删泛用词不能把专有词一起删掉。"""
    from gsuid_core.ai_core.agent_node.registry import match_capability_node
    from gsuid_core.ai_core.capability_agents.profiles import register_builtin_profiles

    register_builtin_profiles()
    assert match_capability_node("帮我出一份本周周报") == "internal_reporter"
    assert match_capability_node("把上个月的数值做个对比") == "internal_reporter"


# ── 内联等回终态后必须 claim，否则双份投递 ────────────────────────────


def test_inline_claim_verdict_silences_when_framework_took_delivery() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import _inline_claim_verdict

    verdict = _inline_claim_verdict(False, 3)
    assert verdict is not None, "没抢到投递标志时必须转静默，否则与执行体双份投递"
    assert "<SILENCE>" in verdict
    assert "#3" in verdict


def test_inline_claim_verdict_allows_delivery_when_claimed() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import _inline_claim_verdict

    assert _inline_claim_verdict(True, 3) is None, "抢到了就照常交回全文"


def test_inline_claim_gate_does_not_reclaim_after_success() -> None:
    """同一次等待已经认领成功后，不得再调一次 claim 把结果改口成静默。"""
    from gsuid_core.ai_core.buildin_tools.subagent import _InlineClaimGate

    calls = {"n": 0}

    def claim() -> bool:
        calls["n"] += 1
        return True

    gate = _InlineClaimGate()
    assert gate.take(claim, 3) is None
    assert gate.take(claim, 3) is None
    assert calls["n"] == 1


def test_claim_is_single_shot() -> None:
    """claim 是读即弃：第二次必须失败，否则两处都会以为自己负责交付。"""
    from gsuid_core.ai_core.planning.kanban_executor import (
        mark_deferred_main_delivery,
        try_claim_deferred_for_inline_return,
    )

    mark_deferred_main_delivery("root-x")
    assert try_claim_deferred_for_inline_return("root-x") is True
    assert try_claim_deferred_for_inline_return("root-x") is False
    assert try_claim_deferred_for_inline_return("never-marked") is False
