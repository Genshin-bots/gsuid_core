"""统一输出闸门 pre_send_gate 单元测试。"""

from __future__ import annotations

from typing import Any
from dataclasses import dataclass


def test_allow_clean_text() -> None:
    from gsuid_core.ai_core.output_gate import GateDecision, pre_send_gate

    r = pre_send_gate("呼…困了zzz", {}, channel="main")
    assert r.decision is GateDecision.ALLOW


def test_angle_bracket_rewrite_then_fuse() -> None:
    from gsuid_core.ai_core.output_gate import (
        GateDecision,
        is_fused,
        pre_send_gate,
        begin_response_batch,
    )
    from gsuid_core.ai_core.angle_bracket_guard import MAX_RETRIES

    extra: dict = {}
    dirty = "点歌？<bubble/>找主人"
    for _ in range(MAX_RETRIES - 1):
        begin_response_batch(extra)
        r = pre_send_gate(dirty, extra, channel="main")
        assert r.decision is GateDecision.REWRITE
        assert r.policy == "angle_bracket"
        assert r.feedback
        assert not is_fused(extra)

    begin_response_batch(extra)
    r3 = pre_send_gate(dirty, extra, channel="main")
    assert r3.decision is GateDecision.FUSE
    assert is_fused(extra)


def test_same_response_batch_counts_one_attempt() -> None:
    """同 ModelResponse 多 TextPart：只计 1 次 attempt，blocked 可多条。"""
    from gsuid_core.ai_core.output_gate import (
        GateDecision,
        attempt_count,
        blocked_texts,
        pre_send_gate,
        begin_response_batch,
    )

    extra: dict = {}
    begin_response_batch(extra)
    r1 = pre_send_gate("aaa<bubble/>", extra, channel="main", count_attempt=True)
    r2 = pre_send_gate("bbb<br>ccc", extra, channel="main", count_attempt=False)
    assert r1.decision is GateDecision.REWRITE
    assert r2.decision is GateDecision.REWRITE
    assert attempt_count(extra, "angle_bracket") == 1
    assert len(blocked_texts(extra, "angle_bracket")) == 2


def test_merge_rewrite_feedbacks() -> None:
    from gsuid_core.ai_core.output_gate import merge_rewrite_feedbacks

    assert merge_rewrite_feedbacks([]) == ""
    assert merge_rewrite_feedbacks(["only"]) == "only"
    m = merge_rewrite_feedbacks(["a", "b"])
    assert "a" in m and "b" in m and "---" in m


def test_plan_angle_after_run_fuse_keeps_ooc_rewrite() -> None:
    """尖括号熔断仍允许独立 OOC 收尾（skip_ooc_rewrite=False）。"""
    from gsuid_core.ai_core.output_gate import (
        set_fused,
        pre_send_gate,
        begin_response_batch,
        plan_angle_after_run,
    )

    extra: dict = {}
    dirty = "x<bubble/>y"
    for _ in range(3):
        begin_response_batch(extra)
        pre_send_gate(dirty, extra, channel="main")
    plan = plan_angle_after_run(extra, clean_sent=[])
    assert plan.fused
    assert plan.drop_blocked
    assert plan.skip_ooc_rewrite is False

    extra2: dict = {}
    begin_response_batch(extra2)
    pre_send_gate(dirty, extra2, channel="main")
    set_fused(extra2)
    plan2 = plan_angle_after_run(extra2, clean_sent=[])
    assert plan2.fused and plan2.skip_ooc_rewrite is False


def test_replace_map_single_blocked_uses_last_clean() -> None:
    from gsuid_core.ai_core.output_gate import (
        pre_send_gate,
        begin_response_batch,
        plan_angle_after_run,
    )

    extra: dict = {}
    begin_response_batch(extra)
    pre_send_gate("脏<br>1", extra, channel="main")
    plan = plan_angle_after_run(extra, clean_sent=["干净回复"])
    assert plan.replace_map == {"脏<br>1": "干净回复"}
    assert not plan.rewrite_original


def test_replace_map_multi_blocked_unequal_clean_only_last() -> None:
    """多脏 + 单干净：只映射最后一条脏文，禁止整表盖成同一句。"""
    from gsuid_core.ai_core.output_gate import (
        pre_send_gate,
        begin_response_batch,
        plan_angle_after_run,
    )

    extra: dict = {}
    begin_response_batch(extra)
    pre_send_gate("aaa<br>", extra, channel="main", count_attempt=True)
    pre_send_gate("bbb<br>", extra, channel="main", count_attempt=False)
    plan = plan_angle_after_run(extra, clean_sent=["only-clean"])
    assert plan.replace_map == {"bbb<br>": "only-clean"}
    assert "aaa<br>" not in plan.replace_map


def test_tool_channel_angle_and_ooc_order() -> None:
    """尖括号优先于 OOC：同时脏时只报尖括号。"""
    from gsuid_core.ai_core.output_gate import GateDecision, pre_send_gate, tool_gate_feedback

    extra: dict = {"turn_id": "t1"}
    text = "我是MiniMax做的<bubble/>助手"
    r = pre_send_gate(text, extra, user_text="你是谁", channel="tool")
    assert r.decision is GateDecision.REWRITE
    assert r.policy == "angle_bracket"

    fb = tool_gate_feedback("zzZ…", extra, user_text="")
    assert fb is None


def test_ooc_main_defers_with_forced_hit(monkeypatch: Any) -> None:
    """不依赖 live config：强制 check_ooc 命中后 main 须 defer + 系统提醒。"""
    from gsuid_core.ai_core import output_firewall as of
    from gsuid_core.ai_core.output_gate import GateDecision, pre_send_gate

    @dataclass
    class _Hit:
        category: str
        matched: list[str]

    monkeypatch.setattr(of, "is_enabled", lambda: True)
    monkeypatch.setattr(
        of,
        "check_ooc",
        lambda text, tier="roleplay", user_text="", exposed_tool_names=(): _Hit("model_identity", ["minimax"]),
    )
    monkeypatch.setattr(of, "build_rewrite_warning", lambda hit: f"warn:{hit.category}")

    extra: dict = {"turn_id": "t-main"}
    r = pre_send_gate("MiniMax呀", extra, user_text="你是什么模型", channel="main")
    assert r.decision is GateDecision.REWRITE
    assert r.policy == "ooc"
    assert r.defer_ooc is True
    assert r.feedback
    assert r.ooc_hit is not None
    # 未注入提醒前，同一句再走 main 仍打回（不是二次发送放行）
    r2 = pre_send_gate("MiniMax呀", extra, user_text="你是什么模型", channel="main")
    assert r2.decision is GateDecision.REWRITE
    assert r2.defer_ooc is True


def test_ooc_main_allows_after_reminder_injected(monkeypatch: Any) -> None:
    """系统提醒送达后，主路径下一句视为模型自判，放行。"""
    from gsuid_core.ai_core import output_firewall as of
    from gsuid_core.ai_core.output_gate import GateDecision, pre_send_gate, mark_ooc_reminded

    @dataclass
    class _Hit:
        category: str
        matched: list[str]

    monkeypatch.setattr(of, "is_enabled", lambda: True)
    monkeypatch.setattr(
        of,
        "check_ooc",
        lambda text, tier="roleplay", user_text="", exposed_tool_names=(): _Hit("model_identity", ["claude"]),
    )
    extra: dict = {"turn_id": "t-judge"}
    first = pre_send_gate("Claude 是 Anthropic 出的", extra, channel="main")
    assert first.decision is GateDecision.REWRITE
    mark_ooc_reminded(extra)
    judged = pre_send_gate("Claude 是 Anthropic 出的", extra, channel="main")
    assert judged.decision is GateDecision.ALLOW
    # 工具通道仍不二次放行
    tool = pre_send_gate("Claude 是 Anthropic 出的", extra, channel="tool")
    assert tool.decision is GateDecision.REWRITE


def test_ooc_warning_is_advisory_not_forced_strip() -> None:
    from gsuid_core.ai_core.output_firewall import OOC_JUDGE_MARKER, FirewallHit, build_rewrite_warning

    w = build_rewrite_warning(FirewallHit(category="model_identity", matched=["claude"]))
    assert OOC_JUDGE_MARKER in w
    assert "自己判断" in w
    assert "去掉任何模型名" not in w


def test_gate_state_is_typed_bag_only() -> None:
    """状态只挂 GateBag，不写旧 angle_bracket_* 键。"""
    from gsuid_core.ai_core.output_gate import _STATE_KEY, GateBag, pre_send_gate

    extra: dict = {}
    pre_send_gate("a<br>b", extra, channel="main")
    assert _STATE_KEY in extra
    assert isinstance(extra[_STATE_KEY], GateBag)
    assert "angle_bracket_attempts" not in extra
    assert "angle_bracket_abort" not in extra


def test_unknown_policy_raises() -> None:
    from gsuid_core.ai_core.output_gate import GateBag, _policy

    try:
        _policy(GateBag(), "not_a_policy")
    except ValueError as e:
        assert "unknown" in str(e).lower() or "not_a_policy" in str(e)
    else:
        raise AssertionError("expected ValueError for unknown policy")


def test_angle_bracket_list_with_br_is_sanitized_not_fused() -> None:
    from gsuid_core.ai_core.output_gate import GateDecision, is_fused, pre_send_gate

    extra: dict = {}
    text = "1. 2024-03-15 Core features<br>2. 2024-04-05 Transaction error handling\n3. 2024-04-25 Security"
    r = pre_send_gate(text, extra, channel="main")
    assert r.decision is GateDecision.FALLBACK
    assert r.policy == "angle_bracket"
    assert "Core features" in r.send_text
    assert "br" not in r.send_text.lower()
    assert "Transaction error handling" in r.send_text
    assert not is_fused(extra)
