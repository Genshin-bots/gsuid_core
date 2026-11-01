"""Phase 4 微弧生命周期：hop 上限、结局映射、过期清理、文本净化、带出结算、footer 解析。"""

import time

from gsuid_core.ai_core.autonomy.models import ParsedRun, AutonomyStepTrace
from gsuid_core.ai_core.autonomy.verifier import parse_footer, take_is_grounded
from gsuid_core.ai_core.autonomy.micro_arc import (
    MicroArcDict,
    prune_arcs,
    advance_arc,
    strip_markup,
    ready_arcs_for,
    normalize_topic_key,
    consolidate_for_target,
)


def _arc_kwargs(**over) -> dict:
    base = dict(
        topic_key="深渊配队",
        intent="查配队",
        origin="self_explore",
        outcome="pending",
        fact="",
        take="",
        target_group_id="",
        target_user_id="",
        max_hops=2,
        now=100.0,
        arc_id="a1",
    )
    base.update(over)
    return base


def test_hop_limit_forces_close_at_max_hops() -> None:
    """同一话题最多跟 max_hops 轮；到轮强制收尾，不会出现 max_hops+1。"""
    arcs, arc = advance_arc([], **_arc_kwargs(max_hops=2))
    assert arc is not None and arc["hop_count"] == 1
    # 第二轮仍 pending，但 hop 到顶 → 有 fact 收尾 confused，无 fact 放弃
    arcs, arc = advance_arc(arcs, **_arc_kwargs(outcome="pending", fact="", arc_id="a2", now=200.0))
    assert arc is not None
    assert arc["hop_count"] == 2
    assert arc["stage"] == "abandoned"
    # 有 fact 时按 confused 收尾
    arcs2, arc2 = advance_arc([], **_arc_kwargs(max_hops=2, fact="结论A", arc_id="b1"))
    arcs2, arc2 = advance_arc(arcs2, **_arc_kwargs(max_hops=2, fact="结论A", arc_id="b2", now=200.0))
    assert arc2 is not None
    assert arc2["stage"] == "ready_to_share" and arc2["outcome"] == "confused"
    assert arc2["expires_at"] == 200.0 + 86400
    # 新话题顶掉旧活跃弧
    arcs3, arc3 = advance_arc(
        [
            {
                "arc_id": "old",
                "topic_key": "旧题",
                "stage": "attempt",
                "origin": "x",
                "intent": "",
                "hop_count": 1,
                "outcome": "pending",
                "fact": "",
                "take": "",
                "target_group_id": "",
                "target_user_id": "",
                "created_at": 0.0,
                "updated_at": 0.0,
                "expires_at": 0.0,
            }
        ],
        **_arc_kwargs(topic_key="新题", arc_id="n1"),
    )
    assert arc3 is not None and arc3["topic_key"] == "新题"
    assert next(a for a in arcs3 if a["topic_key"] == "旧题")["stage"] == "abandoned"


def test_outcome_mapping() -> None:
    """终局结局 + 有 fact/take → ready_to_share；两者皆空 → abandoned。"""
    for outcome in ("found", "confused", "distracted", "wrong_guess"):
        _arcs, arc = advance_arc([], **_arc_kwargs(outcome=outcome, fact="f", take="t", arc_id=outcome))
        assert arc is not None
        assert arc["stage"] == "ready_to_share" and arc["outcome"] == outcome
    _arcs, arc = advance_arc([], **_arc_kwargs(outcome="found", fact="", take="", arc_id="x"))
    assert arc is not None
    assert arc["stage"] == "abandoned"
    # 空 topic 不产生弧
    arcs, arc = advance_arc([], **_arc_kwargs(topic_key="   "))
    assert arc is None and arcs == []


def test_prune_caps_and_expiry() -> None:
    now = 1_000_000.0
    arcs: list[MicroArcDict] = []
    for i in range(5):
        arcs.append(
            {
                "arc_id": f"r{i}",
                "topic_key": f"t{i}",
                "stage": "ready_to_share",
                "origin": "x",
                "intent": "",
                "hop_count": 1,
                "outcome": "found",
                "fact": "f",
                "take": "t",
                "target_group_id": "",
                "target_user_id": "",
                "created_at": now - 10,
                "updated_at": now - 10 * i,
                "expires_at": now + 86400,
            }
        )
    # 一条过期 ready + 一条超龄 consolidated + 两条 active
    arcs[0]["expires_at"] = now - 1
    arcs.append(
        {
            "arc_id": "c",
            "topic_key": "c",
            "stage": "consolidated",
            "origin": "x",
            "intent": "",
            "hop_count": 1,
            "outcome": "found",
            "fact": "",
            "take": "",
            "target_group_id": "",
            "target_user_id": "",
            "created_at": 0.0,
            "updated_at": now - 90000,
            "expires_at": 0.0,
        }
    )
    for i in ("a1", "a2"):
        arcs.append(
            {
                "arc_id": i,
                "topic_key": i,
                "stage": "attempt",
                "origin": "x",
                "intent": "",
                "hop_count": 1,
                "outcome": "pending",
                "fact": "",
                "take": "",
                "target_group_id": "",
                "target_user_id": "",
                "created_at": now - 5,
                "updated_at": now - 5,
                "expires_at": 0.0,
            }
        )
    kept = prune_arcs(arcs, now)
    ready = [a for a in kept if a["stage"] == "ready_to_share"]
    active = [a for a in kept if a["stage"] in ("seed", "attempt")]
    assert len(ready) <= 3
    assert len(active) <= 1
    assert all(a["arc_id"] != "c" for a in kept)
    assert all(a["arc_id"] != "r0" for a in kept)


def test_strip_markup() -> None:
    assert strip_markup("a<search_results><item>x</item></search_results>b") == "a b"
    # 包裹块整体剥掉（含内部文本），lone tag 也剥
    assert strip_markup("<b>bold</b> rest <orphan_tag>") == "rest"
    assert strip_markup("  多  空  ") == "多 空"
    assert normalize_topic_key(" 深渊 配队! ") == "深渊配队"


def test_ready_and_consolidate_scopes() -> None:
    now = time.time()
    group_arc = {
        "arc_id": "g",
        "topic_key": "t",
        "stage": "ready_to_share",
        "origin": "x",
        "intent": "",
        "hop_count": 1,
        "outcome": "found",
        "fact": "f",
        "take": "t",
        "target_group_id": "g1",
        "target_user_id": "u9",
        "created_at": now,
        "updated_at": now,
        "expires_at": now + 86400,
    }
    priv_arc = dict(group_arc, arc_id="p", target_group_id="", target_user_id="u1")
    arcs = [group_arc, priv_arc]
    # 群弧对群可见（不挑 user）；私聊弧只对本人私聊
    assert [a["arc_id"] for a in ready_arcs_for(arcs, "g1", "", now)] == ["g"]
    assert ready_arcs_for(arcs, "g1", "someone_else", now)[0]["arc_id"] == "g"
    assert [a["arc_id"] for a in ready_arcs_for(arcs, "", "u1", now)] == ["p"]
    assert ready_arcs_for(arcs, "", "other", now) == []
    consolidate_for_target(arcs, "g1", "")
    assert group_arc["stage"] == "consolidated"
    assert priv_arc["stage"] == "ready_to_share"


def test_footer_parse_new_format_and_alias() -> None:
    parsed = parse_footer(
        "正文\nDRIVE: care_user\nTARGET: u1\nTOPIC: 深渊配队\nOUTCOME: found\nFACT: 结论\nTAKE: 顺手记的"
    )
    assert parsed.drive == "care_user" and parsed.target == "u1"
    assert parsed.topic == "深渊配队" and parsed.outcome == "found"
    assert parsed.fact == "结论" and parsed.take == "顺手记的"
    assert parsed.summary == "结论"  # delivery 仍走 summary
    assert parsed.trails == ["深渊配队"]
    assert parsed.spoken_text == "正文"
    # SUMMARY 是 FACT 的别名；FACT 优先
    alias = parse_footer("DRIVE: rest\nTARGET: \nSUMMARY: 别名结论")
    assert alias.fact == "别名结论" and alias.summary == "别名结论"
    both = parse_footer("DRIVE: rest\nSUMMARY: 旧\nFACT: 新")
    assert both.fact == "新"
    # 未知 OUTCOME → pending
    bad = parse_footer("DRIVE: rest\nTOPIC: x\nOUTCOME: wat")
    assert bad.outcome == "pending"


def test_take_is_grounded() -> None:
    step_meta = AutonomyStepTrace(0, "find_tools", "args", "out")
    step_real = AutonomyStepTrace(1, "web_search", "args", "results")
    step_err = AutonomyStepTrace(2, "web_fetch", "args", "超时", is_error=True)

    assert take_is_grounded(
        ParsedRun(drive="rest", target="", summary="", trails=[], outcome="confused"), [step_meta, step_err]
    )
    assert not take_is_grounded(
        ParsedRun(drive="rest", target="", summary="", trails=[], outcome="confused"), [step_meta]
    )
    assert take_is_grounded(
        ParsedRun(drive="rest", target="", summary="", trails=[], outcome="found"), [step_meta, step_real]
    )
    assert not take_is_grounded(ParsedRun(drive="rest", target="", summary="", trails=[], outcome="found"), [step_meta])
    assert not take_is_grounded(
        ParsedRun(drive="rest", target="", summary="", trails=[], outcome="pending"), [step_real]
    )


def test_topic_mentioned_matches_beyond_first_24_chars() -> None:
    """话题键出现在长消息尾部也要命中（normalize_topic_key 截 24 字符不够）。"""
    from gsuid_core.ai_core.autonomy.micro_arc import topic_mentioned, normalize_topic_key

    key = normalize_topic_key("演唱会门票")
    long_tail = "今天先聊了三十个字左右完全不相关的事情最后才说到演唱会门票"
    assert topic_mentioned(key, long_tail) is True
    assert topic_mentioned(key, "全是无关内容的闲聊") is False
    assert topic_mentioned("", "演唱会门票") is False
