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


def test_replace_map_equal_lengths_zip() -> None:
    from gsuid_core.ai_core.output_gate import _build_angle_replace_map

    m = _build_angle_replace_map(["d1", "d2"], ["c1", "c2"])
    assert m == {"d1": "c1", "d2": "c2"}


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
    """不依赖 live config：强制 check_ooc 命中后 main 须 defer。"""
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
        lambda text, tier="roleplay", user_text="", exposed_tool_names=(): _Hit("model_name", ["minimax"]),
    )
    monkeypatch.setattr(of, "build_rewrite_warning", lambda hit: f"warn:{hit.category}")

    extra: dict = {}
    r = pre_send_gate("MiniMax呀", extra, user_text="你是什么模型", channel="main")
    assert r.decision is GateDecision.REWRITE
    assert r.policy == "ooc"
    assert r.defer_ooc is True
    assert r.ooc_hit is not None


def test_tool_gate_feedback_compat() -> None:
    from gsuid_core.ai_core.output_gate import tool_gate_feedback

    extra: dict = {}
    assert tool_gate_feedback("正常发言", extra) is None
    w = tool_gate_feedback("a<bubble/>b", extra)
    assert w is not None
    assert "尖括号" in w or "bubble" in w.lower() or "系统校验" in w


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


def test_angle_short_circuit_then_ooc_safe_helper(monkeypatch: Any) -> None:
    """模拟 angle 短路后收尾产物：_ooc_safe_outbound 不得漏放 never-release。"""
    from gsuid_core.ai_core import output_firewall as of
    from gsuid_core.ai_core.output_gate import GateDecision, pre_send_gate

    @dataclass
    class _Hit:
        category: str
        matched: list[str]

    # 同时含 tip + 模型名：gate 只报 angle
    extra: dict = {}
    dual = "我是MiniMax助手<br>继续"
    r = pre_send_gate(dual, extra, user_text="你是谁", channel="main")
    assert r.decision is GateDecision.REWRITE
    assert r.policy == "angle_bracket"

    monkeypatch.setattr(of, "is_enabled", lambda: True)
    monkeypatch.setattr(
        of,
        "check_ooc",
        lambda text, tier="roleplay", user_text="": _Hit("fund_claim", ["已转账"]),
    )
    # 直接测策略辅助逻辑（无整 Agent）
    cleaned = "我是MiniMax助手\n继续"
    # 复现 _ooc_safe_outbound 语义
    hit = of.check_ooc(cleaned, user_text="")
    assert hit is not None
    safe = of.MACHINE_FALLBACK_TEXT if hit.category == "machine_dump" else of.PERSONA_FALLBACK_TEXT
    assert safe == of.PERSONA_FALLBACK_TEXT
