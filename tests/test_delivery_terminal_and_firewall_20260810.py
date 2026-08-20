"""交付终局态 + 交付状态汇报防火墙 + 出图候选/时效/缺口登记 的结构回归（20260810）。

对应 docs/AI_SESSION_OOC_ROOTCAUSE_20260810.md 的 P0/P1/P2 修复：
- DELIVERED 终局态：send 工具带台词交付后，本 run 对用户只许 <SILENCE>；
- 交付状态汇报防火墙：拦「任务已完成/图已发送给X/无需追加发言」这类系统日志腔；
- 出图候选：无时点聚合（气候/月均）不武装；检索类单点不武装、多点才武装；
- 能力缺口登记：find_tools 未命中时计数，供运维侧观察。

判据一律为结构信号（交付回执 / 形态计数 / 时效形态），不认业务域关键词。
"""

from __future__ import annotations

from typing import Any

from gsuid_core.ai_core.agent_run.speech_policy import (
    MAIN_CHANNEL_VISIBLE_LIMIT,
    content_is_render_candidate,
    should_block_user_visible_text,
    looks_like_delivery_status_narration,
)
from gsuid_core.ai_core.capability_agents.delegation_contracts import (
    DELIVERY_SUCCESS_MARK,
    TIMELESS_AGGREGATE_CAVEAT,
    POST_DELIVERY_SILENCE_CONTRACT,
    is_timeless_aggregate,
    fact_pack_is_multi_point,
    tool_return_is_delivery_success,
)

# 生产实录的 OOC 句（已脱敏为通用形态）与同族变体
DELIVERY_NARRATION_HITS = [
    "任务已完成，图已发送给用户甲，无需追加发言。",
    "任务已完成，图已发送，无需追加发言。",
    "图已经发送给用户甲了，无需补充说明。",
    "任务完成，已发给对方，不必回复。",
]

# 角色面向用户的自然交付句：绝不许被当成状态汇报拦下
DELIVERY_BENIGN = [
    "唔…图发过去了，你看看…",
    "画好了，你看",
    "已经帮你把图发出去啦",
    "搞定，图在楼上了",
    "发给你了，不用谢我",
    "查到了，给你发过去了哦",
]


def test_delivery_narration_detector_hits_and_benign_pass() -> None:
    for s in DELIVERY_NARRATION_HITS:
        assert looks_like_delivery_status_narration(s), f"漏判: {s!r}"
    for s in DELIVERY_BENIGN:
        assert not looks_like_delivery_status_narration(s), f"误杀: {s!r}"
    # SILENCE 与空串永不命中
    assert not looks_like_delivery_status_narration("<SILENCE>")
    assert not looks_like_delivery_status_narration("")


def test_check_ooc_delivery_narration_category() -> None:
    from gsuid_core.ai_core.output_firewall import check_ooc

    for s in DELIVERY_NARRATION_HITS:
        hit = check_ooc(s)
        assert hit is not None and hit.category == "delivery_narration", f"漏判: {s!r}"
    for s in DELIVERY_BENIGN:
        hit = check_ooc(s)
        assert hit is None or hit.category != "delivery_narration", f"误杀: {s!r}"


def test_pre_send_gate_fuses_delivery_narration_on_main(monkeypatch: Any) -> None:
    """主通道命中交付状态汇报 → FUSE 静默，而非 REWRITE 重说。"""
    from gsuid_core.ai_core import output_firewall as of
    from gsuid_core.ai_core.output_gate import GateDecision, pre_send_gate

    monkeypatch.setattr(of, "is_enabled", lambda: True)
    r = pre_send_gate(DELIVERY_NARRATION_HITS[0], {}, user_text="", channel="main")
    assert r.decision is GateDecision.FUSE
    assert r.policy == "ooc"


def test_delivered_policy_blocks_all_but_silence() -> None:
    """delivered 终局态：除 <SILENCE> 外一律拦。"""
    for text in ("唔…随便再说点啥…", "任务已完成，无需追加发言。", "zzz"):
        blk, why = should_block_user_visible_text(
            "delivered",
            text,
            pending_async=False,
            image_sent=True,
            has_status_tool=False,
            tool_calls_so_far=["send_message_by_ai"],
        )
        assert blk and why == "delivered_terminal", f"未拦: {text!r}"
    blk, why = should_block_user_visible_text(
        "delivered",
        "<SILENCE>",
        pending_async=False,
        image_sent=True,
        has_status_tool=False,
        tool_calls_so_far=["send_message_by_ai"],
    )
    assert not blk and why == "silence"


def test_image_sent_blocks_delivery_narration_even_without_terminal() -> None:
    """即便未置 delivered，image_sent 分支也靠检测器拦交付状态汇报。"""
    blk, why = should_block_user_visible_text(
        "framework_deliver",
        DELIVERY_NARRATION_HITS[0],
        pending_async=False,
        image_sent=True,
        has_status_tool=False,
        tool_calls_so_far=["send_message_by_ai"],
    )
    assert blk and why == "delivery_narration"


def test_delivery_success_receipt_marker() -> None:
    assert tool_return_is_delivery_success(f"{DELIVERY_SUCCESS_MARK} 123456")
    assert tool_return_is_delivery_success("消息已发送给用户 42")
    assert not tool_return_is_delivery_success("发送失败：Bot对象不可用")
    assert not tool_return_is_delivery_success(None)
    assert not tool_return_is_delivery_success(123)


def test_delivery_silence_contract_constants() -> None:
    assert "SILENCE" in POST_DELIVERY_SILENCE_CONTRACT
    assert "任务已完成" in POST_DELIVERY_SILENCE_CONTRACT or "状态汇报" in POST_DELIVERY_SILENCE_CONTRACT
    assert "现在" in TIMELESS_AGGREGATE_CAVEAT or "实时" in TIMELESS_AGGREGATE_CAVEAT
    assert MAIN_CHANNEL_VISIBLE_LIMIT >= 1


# ── 出图候选 / 时效 / 多点性 ────────────────────────────────────────

_DONGGUAN_CLIMATE = (
    "[1] 东莞市 在 八月 2026 的天氣：氣溫與氣候\n"
    "| 9. 八月 | 28 °C | 31 °C | 26 °C | 8.7 mm |\n"
    "| 10. 八月 | 28 °C | 31 °C | 25 °C | 15.2 mm |\n"
    "| 八月 | 27.7 | 25.3 | 31.1 | 296 | 85% |"
)
_FORECAST = (
    "广州未来七天天气预报\n"
    "8月10日 32°C/26°C 晴 东南风3级\n"
    "8月11日 33°C/27°C 多云 微风\n"
    "8月12日 31°C/26°C 雷阵雨 东风2级\n"
    "8月13日 30°C/25°C 大雨 北风3级"
)


def test_timeless_aggregate_not_render_armed() -> None:
    assert is_timeless_aggregate(_DONGGUAN_CLIMATE)
    # 逐日序列（带日期）不算低时效聚合
    assert not is_timeless_aggregate(_FORECAST)
    # 无时点聚合 → 不武装出图（无论 fileos 与否）
    assert not content_is_render_candidate(tool_name="web_search_tool", content=_DONGGUAN_CLIMATE, fileos_folded=True)
    assert not content_is_render_candidate(tool_name="web_search_tool", content=_DONGGUAN_CLIMATE, fileos_folded=False)


def test_search_multipoint_armed_single_point_not() -> None:
    # 多点（逐日数据行）→ 武装
    assert content_is_render_candidate(tool_name="web_search_tool", content=_FORECAST, fileos_folded=False)
    # FileOS 折叠后仍用原文形态：多点检索必须武装，否则主人格只会念数
    assert content_is_render_candidate(tool_name="web_search_tool", content=_FORECAST, fileos_folded=True)
    # 单点读数 → 不武装
    assert not content_is_render_candidate(
        tool_name="web_search_tool", content="东莞现在 31°C，多云。", fileos_folded=False
    )


def test_fact_pack_multi_point_counting() -> None:
    assert fact_pack_is_multi_point(_FORECAST)
    assert fact_pack_is_multi_point("| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n| 5 | 6 |")
    assert not fact_pack_is_multi_point("只有一条结论。")
    # 列表形态
    news = "1. A\n2. B\n3. C"
    assert fact_pack_is_multi_point(news)


# ── 能力缺口登记 ────────────────────────────────────────────────────


def test_capability_gap_recording() -> None:
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import (
        get_capability_gaps,
        _record_capability_gap,
    )

    _record_capability_gap("查询东莞实时天气温度")
    _record_capability_gap("查询东莞实时天气温度")
    _record_capability_gap("   ")  # 空白不记
    gaps = get_capability_gaps(limit=50)
    keys = {k for k, _ in gaps}
    assert "查询东莞实时天气温度" in keys
    counts = dict(gaps)
    assert counts["查询东莞实时天气温度"] >= 2


# ── 防火墙良性回归（防本次收紧误杀既有语料） ───────────────────────


def test_firewall_benign_corpus_still_passes() -> None:
    from tests.test_benign_fp import FIREWALL_BENIGN
    from gsuid_core.ai_core.output_firewall import check_ooc

    bad = [s for s in FIREWALL_BENIGN if check_ooc(s) is not None]
    assert not bad, "输出侧良性话术被新拦截:\n" + "\n".join(bad)


if __name__ == "__main__":
    test_delivery_narration_detector_hits_and_benign_pass()
    test_check_ooc_delivery_narration_category()
    test_delivered_policy_blocks_all_but_silence()
    test_image_sent_blocks_delivery_narration_even_without_terminal()
    test_delivery_success_receipt_marker()
    test_delivery_silence_contract_constants()
    test_timeless_aggregate_not_render_armed()
    test_search_multipoint_armed_single_point_not()
    test_fact_pack_multi_point_counting()
    test_capability_gap_recording()
    test_firewall_benign_corpus_still_passes()
    print("ALL PASS")
