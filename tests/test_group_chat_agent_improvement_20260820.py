"""群聊人设 Agent 改进（2026-08-20）结构回归。"""

from __future__ import annotations

from pydantic_ai.messages import ModelRequest, UserPromptPart

from gsuid_core.ai_core.kits.base import BLOCK_CHAR_BUDGET, CONTEXT_BLOCK_ORDER, join_named_blocks
from gsuid_core.ai_core.output_gate import GateDecision, pre_send_gate
from gsuid_core.ai_core.prefix_probe import (
    PrefixSnapshot,
    hash_text,
    classify_prefix_break,
    reset_prefix_break_counts,
)
from gsuid_core.ai_core.schema_brief import SCHEMA_BRIEF_MAX, make_schema_brief
from gsuid_core.ai_core.pocket_planner import (
    build_plan_hint,
    _format_eval_plan,
    compose_plan_hint,
    should_plan_first,
)
from gsuid_core.ai_core.memory.observer import parse_meme_explain, detect_meme_explain_intent
from gsuid_core.ai_core.output_firewall import check_ooc
from gsuid_core.message_history.manager import MessageRecord
from gsuid_core.ai_core.agent_run.settle import _dedupe_delivery_cards
from gsuid_core.ai_core.heartbeat.decision import (
    ACTIVE_WINDOW_MIN,
    MASTER_ACTIVE_WINDOW_MIN,
    _build_masters_section,
    _pick_recent_speaker_names,
)
from gsuid_core.ai_core.agent_run.speech_policy import should_block_user_visible_text


def test_block_order_has_group_context_and_plan_hint() -> None:
    assert CONTEXT_BLOCK_ORDER.index("history") < CONTEXT_BLOCK_ORDER.index("group_context")
    assert CONTEXT_BLOCK_ORDER.index("group_context") < CONTEXT_BLOCK_ORDER.index("memory")
    assert CONTEXT_BLOCK_ORDER.index("task") < CONTEXT_BLOCK_ORDER.index("plan_hint")
    assert "group_context" in BLOCK_CHAR_BUDGET
    assert BLOCK_CHAR_BUDGET["group_context"] == 200
    assert BLOCK_CHAR_BUDGET["memory"] == 800


def test_join_named_blocks_truncates_over_budget() -> None:
    huge = "啊" * 500
    out = join_named_blocks({"mood": huge, "history": "H"})
    assert "H" in out
    mood = out.split("\n\n")[0]
    assert len(mood) <= BLOCK_CHAR_BUDGET["mood"]


def test_schema_brief_keeps_first_paragraph() -> None:
    doc = "查询公开资料。\n\nArgs:\n    symbol: 代码\nReturns:\n    报价文本"
    brief = make_schema_brief(doc)
    assert "查询公开资料" in brief
    assert "Returns" not in brief
    assert len(brief) <= SCHEMA_BRIEF_MAX
    assert make_schema_brief(doc, explicit="短说查价") == "短说查价"


def test_inner_os_gate_blocks_long_monologue() -> None:
    extra: dict[str, object] = {}
    leak = "（心想：该说点啥…不对…加点角色味道…有点太普通了…再加点…好想再看一遍…可以…短一点…唔…）唔…看过"
    r = pre_send_gate(leak, extra, channel="main")
    assert r.decision is GateDecision.REWRITE
    assert r.policy == "inner_os"
    r2 = pre_send_gate(leak, extra, channel="main")
    assert r2.decision in (GateDecision.FALLBACK, GateDecision.FUSE, GateDecision.ALLOW)


def test_inner_os_gate_catches_mid_sentence_and_restrips() -> None:
    extra: dict[str, object] = {}
    leak = "唔…看过（心想：该说点啥…不对…再想想怎么开口才像本人…短一点比较好…）好吧"
    r = pre_send_gate(leak, extra, channel="main")
    assert r.decision is GateDecision.REWRITE
    r2 = pre_send_gate(leak, extra, channel="main")
    assert r2.decision is GateDecision.FALLBACK
    assert "心想" not in r2.send_text
    assert "好吧" in r2.send_text


def test_inner_os_gate_allows_short_stage_direction() -> None:
    extra: dict[str, object] = {}
    r = pre_send_gate("（揉揉眼睛）唔…困", extra, channel="main")
    assert r.decision is GateDecision.ALLOW


def test_dev_vocab_ooc_hits_tool_failure_speech() -> None:
    hit = check_ooc("唔…那个接口还没配好…发不了…")
    assert hit is not None
    assert hit.category == "dev_vocab"


def test_dev_vocab_whitelist_stat_口径() -> None:
    hit = check_ooc("这项指标 0.79%（数据口径）")
    assert hit is None or hit.category != "dev_vocab"


def test_heartbeat_master_freshness_stale_forbids_direct_address() -> None:
    now = 1_000_000.0
    history = [
        MessageRecord(role="user", content="hi", user_id="master1", user_name="主人", timestamp=now - 3 * 3600),
        MessageRecord(role="user", content="晚", user_id="other", user_name="群友", timestamp=now - 60),
    ]
    from unittest.mock import patch

    with patch("gsuid_core.ai_core.heartbeat.decision.core_config") as cfg:
        cfg.get_config.return_value = ["master1"]
        text = _build_masters_section(history, now)
    assert "很可能不在看群" in text
    assert "一律不许" in text
    assert "早睡了" not in text
    assert MASTER_ACTIVE_WINDOW_MIN == 30
    assert ACTIVE_WINDOW_MIN == 30


def test_prefix_break_classifies_tools_and_system() -> None:
    reset_prefix_break_counts()
    prev = PrefixSnapshot(
        history_hashes=["aaaa"],
        tools_hash=hash_text("a\nb"),
        system_hash=hash_text("sys1"),
        payloads=["user:hi"],
    )
    assert (
        classify_prefix_break(
            prev,
            history_hashes=["aaaa"],
            tools_hash=hash_text("a\nb"),
            system_hash=hash_text("sys2"),
        )
        == "system"
    )
    assert (
        classify_prefix_break(
            prev,
            history_hashes=["aaaa"],
            tools_hash=hash_text("a\nc"),
            system_hash=hash_text("sys1"),
        )
        == "tools"
    )


def test_pick_recent_speakers_keeps_latest_not_first_seen() -> None:
    last = {
        "old": (1.0, "甲"),
        "mid": (50.0, "乙"),
        "new": (100.0, "丙"),
    }
    for i in range(8):
        last[f"e{i}"] = (10.0 + i, f"n{i}")
    names = _pick_recent_speaker_names(last, limit=8)
    assert "new" in names
    assert "mid" in names
    assert "old" not in names


def test_pocket_planner_idle_chitchat_does_not_plan() -> None:
    assert not should_plan_first("困", recent_eval=False)


def test_pocket_planner_triggers_on_multi_task() -> None:
    assert should_plan_first("帮我查一下资料然后顺便整理一份")
    assert should_plan_first("帮我安排明天的日程")
    assert not should_plan_first("困")
    hint = build_plan_hint("任意原问")
    assert "本轮计划" in hint
    assert "find_tools" in hint


def test_meme_explain_detect_and_parse() -> None:
    assert detect_meme_explain_intent("蓝框是某某活动的梗")
    parsed = parse_meme_explain("蓝框是某某活动的梗，可接一句")
    assert parsed is not None
    assert parsed[0] == "蓝框"
    assert "某某活动" in parsed[1]


def test_meme_explain_ignores_everyday_meaning() -> None:
    assert not detect_meme_explain_intent("这句话的意思是我不同意")
    assert not detect_meme_explain_intent("出自真心")
    assert not detect_meme_explain_intent("这是你的意思吗")
    assert not detect_meme_explain_intent("我的意思是明天再去")
    assert parse_meme_explain("这句话的意思是我不同意") is None


def test_persona_length_does_not_swallow_long_speech() -> None:
    blocked, why = should_block_user_visible_text(
        "free",
        "啊" * 200,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=(),
        speech_len_hard=150,
        user_asked_detail=False,
    )
    assert blocked is False
    assert why == "ok"


def test_dedupe_delivery_cards_keeps_latest_dlg() -> None:
    a = ModelRequest(parts=[UserPromptPart(content="超时回执 dlg_aaaaaaaa")])
    b = ModelRequest(parts=[UserPromptPart(content="事后交付 dlg_aaaaaaaa 产物 res_1")])
    c = ModelRequest(parts=[UserPromptPart(content="普通话")])
    out = _dedupe_delivery_cards([a, b, c])
    texts = [p.content for m in out for p in m.parts if isinstance(p, UserPromptPart)]
    assert any("事后交付" in str(t) for t in texts)
    assert not any("超时回执" in str(t) for t in texts)


def test_self_ontology_template_mentions_host() -> None:
    from unittest.mock import patch

    from gsuid_core.ai_core.self_cognition import default_self_ontology

    with patch("gsuid_core.config.core_config") as cfg:
        cfg.get_config.return_value = []
        text = default_self_ontology()
    assert "宿主" in text
    assert "插件" in text
    assert "角色卡" in text
    assert "GsCore" in text
    assert "结界" not in text
    assert "忍具" not in text


def test_frozen_tool_message_stop_loss() -> None:
    from gsuid_core.ai_core.tool_health import frozen_tool_message

    msg = frozen_tool_message("stock_indicators")
    assert "停用" in msg
    assert "stock_indicators" in msg


def test_post_tool_fail_contract_has_roleplay_clause() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import POST_TOOL_FAIL_CONTRACT

    assert "开发者词汇" in POST_TOOL_FAIL_CONTRACT
    assert "角色卡" in POST_TOOL_FAIL_CONTRACT
    assert "情报网" not in POST_TOOL_FAIL_CONTRACT
    assert "卷轴" not in POST_TOOL_FAIL_CONTRACT


def test_parse_session_scope_group_and_private() -> None:
    from gsuid_core.ai_core.session_registry import parse_session_scope

    bot, kind, sid = parse_session_scope("ws:adapter:selfbot:group:g9001")
    assert bot == "selfbot"
    assert kind == "group"
    assert sid == "g9001"
    bot2, kind2, sid2 = parse_session_scope("ws:adapter:selfbot:private:u9002")
    assert (bot2, kind2, sid2) == ("selfbot", "private", "u9002")
    assert parse_session_scope("odd") == ("", "", "")


def test_decision_rule_summary_has_three_parts() -> None:
    from gsuid_core.ai_core.kits.decision_distill.kit import _Pending, _rule_summary, _parse_distill_json

    text = _rule_summary(_Pending(thinking="先查再汇总", tools=["find_tools"], result="唔…查完了", bot_self_id="b1"))
    assert "决策：" in text
    assert "理由：" in text
    assert "结局：" in text
    parsed = _parse_distill_json(
        '[{"decision":"委派 stock_agent","rationale":"组合查询","outcome":"已出图"}]',
        1,
    )
    assert len(parsed) == 1
    assert "委派 stock_agent" in parsed[0]


def test_compose_plan_hint_reuses_recent_eval() -> None:
    import asyncio

    from gsuid_core.ai_core.capability_agents.evaluator import (
        SuggestedSubtask,
        CapabilityEvaluationResult,
        record_evaluation,
    )

    record_evaluation(
        CapabilityEvaluationResult(
            covered=True,
            summary="先检索再呈现",
            suggested_subtasks=[
                SuggestedSubtask(
                    description="find_tools 查资料",
                    required_capability="research",
                    agent_profile="research_agent",
                )
            ],
            owner_user_id="u_synth_plan",
            user_goal="帮我查一下资料然后顺便整理一份",
        )
    )
    hint = asyncio.run(compose_plan_hint("帮我查一下资料然后顺便整理一份", "u_synth_plan"))
    assert "沿用近 1h 评估" in hint
    assert "查资料" in hint
    assert "本轮计划" in _format_eval_plan("摘要", ["步骤甲"])
