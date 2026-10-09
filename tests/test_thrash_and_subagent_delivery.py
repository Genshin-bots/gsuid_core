"""thrash 跨轮计数 + 能力代理过程句检测回归。"""

from __future__ import annotations


def test_thrash_same_response_parallel_counts_one_turn() -> None:
    from gsuid_core.ai_core.gs_agent import _update_thrash_streak_for_response

    # 同响应 4 次 web_search → 只 +1 轮
    name, streak = _update_thrash_streak_for_response(
        ["web_search_tool"] * 4,
        prev_name="",
        prev_streak=0,
    )
    assert name == "web_search_tool"
    assert streak == 1

    name2, streak2 = _update_thrash_streak_for_response(
        ["web_search_tool"] * 3,
        prev_name=name,
        prev_streak=streak,
    )
    assert name2 == "web_search_tool"
    assert streak2 == 2


def test_thrash_mixed_tools_resets() -> None:
    from gsuid_core.ai_core.gs_agent import _update_thrash_streak_for_response

    name, streak = _update_thrash_streak_for_response(
        ["web_search_tool", "web_fetch_tool"],
        prev_name="web_search_tool",
        prev_streak=3,
    )
    assert name == ""
    assert streak == 0


def test_thrash_empty_response_keeps_streak() -> None:
    from gsuid_core.ai_core.gs_agent import _update_thrash_streak_for_response

    name, streak = _update_thrash_streak_for_response(
        [],
        prev_name="web_search_tool",
        prev_streak=2,
    )
    assert name == "web_search_tool"
    assert streak == 2


def test_post_tool_contracts_split_persona_vs_capability() -> None:
    from gsuid_core.ai_core.gs_agent import (
        _POST_TOOL_OUTPUT_CONTRACT,
        _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
        _post_tool_contracts_for,
    )

    ok, fail = _post_tool_contracts_for("Chat")
    assert ok is _POST_TOOL_OUTPUT_CONTRACT
    assert "render_agent" in ok
    assert "render_" in ok or "render_agent" in ok

    ok_c, fail_c = _post_tool_contracts_for("CapabilityAgent")
    assert ok_c is _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY
    assert "事实包" in ok_c or "Markdown" in ok_c
    assert "禁止" in ok_c and "render_html" in ok_c
    assert "render_html_to_image" not in fail_c or "禁止" in fail_c


def test_delegation_rejects_other_speakers_history_topic() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import delegation_grounded

    said = "帮忙比较一下样本甲组、样本乙组和样本丙组"
    assert delegation_grounded("对比样本甲组、样本乙组、样本丙组的要点", said)
    assert not delegation_grounded("分析样本丁组并给出方案", said)
    assert not delegation_grounded("深度分析样本戊组", said)
    assert delegation_grounded("甲组配置里，成员乙已经带了丙类部件", "甲组配置，成员乙已经带了丙类部件，成员丁带什么")
    assert not delegation_grounded("随便查点别的", "救我")
    assert not delegation_grounded("analyze the market", "help me with the report")
    assert delegation_grounded("compare sample alpha", "please compare sample alpha today")


def test_deictic_to_recent_media_uses_upstairs_image(monkeypatch) -> None:
    from gsuid_core.models import Event
    from gsuid_core.message_history.manager import HistoryManager
    from gsuid_core.ai_core.buildin_tools.subagent import (
        deictic_to_recent_media,
        delegation_ground_reject,
    )

    mgr = HistoryManager()
    image_ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g9001",
        user_id="u2",
        WS_BOT_ID="ws1",
    )
    note_ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g9001",
        user_id="u3",
        WS_BOT_ID="ws1",
    )
    ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g9001",
        user_id="u1",
        WS_BOT_ID="ws1",
        msg_id="cur-1",
    )
    mgr.add_message(
        image_ev,
        "user",
        "（一张图）",
        user_name="乙",
        metadata={"image_id": "img_synth_who_01", "msg_id": "img-1"},
    )
    mgr.add_message(note_ev, "user", "你们看", user_name="丙", metadata={"msg_id": "txt-1"})
    mgr.add_message(ev, "user", "看看这是谁", user_name="甲", metadata={"msg_id": "cur-1"})
    monkeypatch.setattr("gsuid_core.message_history.get_history_manager", lambda: mgr)

    ev.raw_text = "看看这是谁"
    assert deictic_to_recent_media(ev)
    assert delegation_ground_reject(ev, "看图里是谁", follow=False) is None
    assert delegation_ground_reject(ev, "分析样本丁组并给出方案", follow=False) is not None

    for raw in (
        "@早柚 看看这是谁",
        "小明(用户ID:9001)：@早柚 看看这是谁",
        "小明(用户ID:9001)：看看这是谁",
    ):
        ev.raw_text = raw
        assert deictic_to_recent_media(ev), raw
        assert delegation_ground_reject(ev, "看图里是谁", follow=False) is None, raw

    monkeypatch.setattr(
        "gsuid_core.ai_core.persona.config.persona_config_manager.get_persona_for_session",
        lambda _sid: "早柚",
    )
    ev.raw_text = "早柚 看看这是谁"
    assert deictic_to_recent_media(ev)
    ev.raw_text = "@早柚看看这是谁"
    assert deictic_to_recent_media(ev)

    for raw in ("这是谁的错", "这图书馆几点开门", "什么图书馆", "这图纸发我", "看这天气", "这张表怎么看", "锐评一下"):
        ev.raw_text = raw
        assert not deictic_to_recent_media(ev), raw

    far = HistoryManager()
    far.add_message(
        image_ev,
        "user",
        "（一张图）",
        user_name="乙",
        metadata={"image_id": "img_synth_who_01", "msg_id": "far-img"},
    )
    far.add_message(note_ev, "user", "第一句", user_name="丙", metadata={"msg_id": "far-a"})
    far.add_message(note_ev, "user", "第二句", user_name="丙", metadata={"msg_id": "far-b"})
    far.add_message(ev, "user", "看看这是谁", user_name="甲", metadata={"msg_id": "cur-1"})
    monkeypatch.setattr("gsuid_core.message_history.get_history_manager", lambda: far)
    ev.raw_text = "看看这是谁"
    assert not deictic_to_recent_media(ev)


def test_deictic_sees_captionless_inbound_image(monkeypatch) -> None:
    from gsuid_core.models import Event
    from gsuid_core.handler import _record_inbound_history
    from gsuid_core.message_history.manager import HistoryManager
    from gsuid_core.ai_core.buildin_tools.subagent import (
        deictic_to_recent_media,
        delegation_ground_reject,
    )

    mgr = HistoryManager()
    monkeypatch.setattr("gsuid_core.message_history.get_history_manager", lambda: mgr)

    image_ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g9001",
        user_id="u2",
        WS_BOT_ID="ws1",
        msg_id="img-1",
    )
    image_ev.image_id = "img_synth_who_01"
    image_ev.raw_text = ""
    _record_inbound_history(image_ev)

    skip_ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g9001",
        user_id="u3",
        WS_BOT_ID="ws1",
        msg_id="skip-1",
    )
    skip_ev.raw_text = ""
    _record_inbound_history(skip_ev)
    assert mgr.get_history(skip_ev, limit=8)[-1].metadata["image_id"] == "img_synth_who_01"

    ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g9001",
        user_id="u1",
        WS_BOT_ID="ws1",
        msg_id="cur-1",
    )
    ev.raw_text = "@早柚 看看这是谁"
    _record_inbound_history(ev)
    assert deictic_to_recent_media(ev)
    assert delegation_ground_reject(ev, "看图里是谁", follow=False) is None


def test_correction_pass_repoints_host_tool_calls_to_parent() -> None:
    """纠正成功后宿主指针指回并好的父级列表；失败则拨回，且不并入纠正轮的名字。"""
    import asyncio

    from pydantic_ai.usage import UsageLimits

    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.agent_run.state import RunOnceState
    from gsuid_core.ai_core.control.corrections import numeric_recitation_directive

    def _agent() -> GsCoreAIAgent:
        agent = object.__new__(GsCoreAIAgent)
        agent._last_attempt_delegated_render = False
        agent._last_attempt_image_sent = False
        agent._last_attempt_pending_async = False
        agent._last_attempt_has_status_tool = False
        return agent

    def _parent() -> tuple[GsCoreAIAgent, RunOnceState]:
        agent = _agent()
        st = RunOnceState(
            user_message="总结一下",
            bot=None,
            ev=None,
            rag_context=None,
            tools=[],
            return_mode="return",
            output_type=None,
            intent=None,
            has_active_task=False,
            budget_gate=False,
            suppress_intermediate_text=False,
            fake_done_retry=False,
            turn_graph=None,
            cheap_gate=None,
            is_framework_injection=False,
        )
        st.limits = UsageLimits(request_limit=20)
        st.tool_call_list = ["search_cognition"]
        agent._last_attempt_tool_calls = st.tool_call_list
        return agent, st

    agent, st = _parent()

    async def _ok(*_args: object, **_kwargs: object) -> str:
        agent._last_attempt_tool_calls = ["web_search"]
        return "rewritten"

    agent._execute_run_once = _ok
    result = asyncio.run(agent._try_correction_pass(st, (numeric_recitation_directive(),)))
    assert result == "rewritten"
    assert st.tool_call_list == ["search_cognition", "web_search"]
    assert agent._last_attempt_tool_calls is st.tool_call_list

    agent, st = _parent()

    async def _boom(*_args: object, **_kwargs: object) -> str:
        agent._last_attempt_tool_calls = []
        raise RuntimeError("provider down")

    agent._execute_run_once = _boom
    failed = asyncio.run(agent._try_correction_pass(st, (numeric_recitation_directive(),)))
    assert failed is None
    assert st.tool_call_list == ["search_cognition"]
    assert agent._last_attempt_tool_calls is st.tool_call_list


def test_production_delegation_does_not_inline_wait() -> None:
    """群聊委派不得占主 session 锁等待子代理；结论只走框架回灌。"""
    from gsuid_core.ai_core.buildin_tools.subagent import (
        _KANBAN_TEST_WAIT_TIMEOUT_SEC,
        _kanban_wait_sec,
    )

    assert _kanban_wait_sec("Chat") == 0.0
    assert _kanban_wait_sec("Agent") == 0.0
    assert _kanban_wait_sec("") == 0.0
    assert _kanban_wait_sec("TEST") == _KANBAN_TEST_WAIT_TIMEOUT_SEC


def test_post_tool_contract_does_not_rival_delegation_with_research() -> None:
    """再搜不点名聚合节点，也不把换 query 写成下一步。"""
    from gsuid_core.ai_core.capability_agents.delegation_contracts import POST_TOOL_OUTPUT_CONTRACT

    assert "internal_reporter" not in POST_TOOL_OUTPUT_CONTRACT
    assert "类目词" not in POST_TOOL_OUTPUT_CONTRACT
    assert "换更具体的词" in POST_TOOL_OUTPUT_CONTRACT
    assert "或换 query 再搜" not in POST_TOOL_OUTPUT_CONTRACT
    assert "换描述再 find_tools" not in POST_TOOL_OUTPUT_CONTRACT


def test_deferred_ack_closes_serial_followup() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        DELEGATION_FANOUT_RULE,
        DELEGATION_INFLIGHT_KEY,
        PENDING_DELEGATION_HOLD,
        delegation_is_inflight,
        format_deferred_subagent_ack,
    )

    assert not delegation_is_inflight({})
    assert not delegation_is_inflight({DELEGATION_INFLIGHT_KEY: False})
    assert delegation_is_inflight({DELEGATION_INFLIGHT_KEY: True})
    ack = format_deferred_subagent_ack(ordinal=64, pid="research_agent", handle="dlg_x")
    assert "task#64" in ack
    assert DELEGATION_FANOUT_RULE in ack
    assert "逐个补派" in ack
    assert "render_agent" in ack
    assert "<SILENCE>" in PENDING_DELEGATION_HOLD
    assert DELEGATION_FANOUT_RULE in PENDING_DELEGATION_HOLD


def test_research_agent_not_default_transient() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import _TRANSIENT_DEFAULT_PROFILES

    assert "research_agent" not in _TRANSIENT_DEFAULT_PROFILES
    assert "internal_reporter" in _TRANSIENT_DEFAULT_PROFILES


def test_interactive_main_ignores_model_transient_flag() -> None:
    from inspect import getsource

    from gsuid_core.ai_core.buildin_tools import subagent as sub

    src = getsource(sub._create_subagent_impl)
    assert "allow_user_outbound" in src
    assert "use_transient = pid in _TRANSIENT_DEFAULT_PROFILES" in src


def test_incomplete_delivery_detects_process_only() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import (
        looks_like_incomplete_subagent_delivery,
    )

    assert looks_like_incomplete_subagent_delivery(
        "收到，停止重复调用。下面再做几次差异化的关键搜索补全本周事件，然后渲染HTML周报图。"
    )
    assert looks_like_incomplete_subagent_delivery("")
    assert looks_like_incomplete_subagent_delivery(
        "【research_agent 临时代理已完成 / transient 模式】（**未在看板创建任务卡**——lookup 模式。）"
        "主人格：角色短句结论 + 数据用 render_html_to_image 出图，禁止整段念出。\n\n"
        "收到，停止重复调用。下面再做几次。"
    )


def test_incomplete_delivery_accepts_fact_package() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import (
        looks_like_incomplete_subagent_delivery,
    )

    md = """# 金融周报 2026-07-27~08-03

## 条目
1. **2026-07-29 FOMC** 维持利率 3.50-3.75%。来源：federalreserve.gov
2. **2026-07-30 政治局会议** 部署下半年经济。来源：新华社
3. **2026-07-31 中国 PMI** 制造业 49.2%。来源：国家统计局

## 依据
- web_search_tool / get_latest_news
"""
    assert not looks_like_incomplete_subagent_delivery(md)


def test_incomplete_delivery_accepts_res_handle_summary() -> None:
    """artifact 短摘要含 res_ 不得判 incomplete（交付误杀回归）。"""
    from gsuid_core.ai_core.buildin_tools.subagent import (
        looks_like_incomplete_subagent_delivery,
    )

    summary = (
        "事实包已登记为 **`res_fa2c9a5b1364`**（22,282 字节，text/markdown）。请主persona把句柄转给 render_agent。"
    )
    assert not looks_like_incomplete_subagent_delivery(summary)
    assert not looks_like_incomplete_subagent_delivery(
        "【research_agent 临时代理已完成 / transient 模式】\n\n" + summary
    )


def test_ooc_scrub_kills_res_handle_but_capability_path_must_not() -> None:
    """roleplay scrub 会杀 res_（丢弃整句，不罐头代答）；能力代理 return 不得走该路径。"""
    from gsuid_core.ai_core.output_firewall import check_ooc, scrub_or_drop

    sample = "事实包已登记为 **`res_fa2c9a5b1364`**，请转 render_agent。"
    hit = check_ooc(sample)
    assert hit is not None
    out, scrubbed = scrub_or_drop(sample)
    assert scrubbed is True
    assert out == ""


def test_followup_task_mentions_no_render() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import _delivery_followup_task

    t = _delivery_followup_task("整理近一周金融新闻")
    assert "事实包" in t
    assert "render_" in t
    assert "整理近一周" in t
