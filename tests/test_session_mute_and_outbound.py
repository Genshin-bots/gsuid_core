"""会话静默 + 能力代理禁止出站 + receipt 图语义 + 节点匹配。"""

from __future__ import annotations


def test_session_mute_lifecycle() -> None:
    from gsuid_core.ai_core.session_mute import (
        is_session_muted,
        set_session_mute,
        clear_session_mute,
        mute_remaining_sec,
    )

    sid = "test_session_mute_unit"
    clear_session_mute(sid)
    assert not is_session_muted(sid)
    set_session_mute(sid, 120.0)
    assert is_session_muted(sid)
    assert mute_remaining_sec(sid) > 100
    clear_session_mute(sid)
    assert not is_session_muted(sid)


def test_receipt_image_requires_image_art() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        receipt_image_likely,
    )

    assert not receipt_image_likely(pid="render_agent", has_image_art=False)
    assert receipt_image_likely(pid="any", has_image_art=True)


def test_match_capability_node_empty_without_hit() -> None:
    from gsuid_core.ai_core.agent_node.registry import match_capability_node
    from gsuid_core.ai_core.capability_agents.profiles import register_builtin_profiles

    register_builtin_profiles()
    assert match_capability_node("完全无关的闲聊你好呀") == ""
    assert match_capability_node("出图做对比表") == "render_agent"


def test_tool_context_default_allows_outbound() -> None:
    from gsuid_core.ai_core.models import ToolContext

    ctx = ToolContext()
    assert ctx.allow_user_outbound is True


def test_delivery_boundary_forbids_all_direct_send() -> None:
    from gsuid_core.ai_core.agent_node.models import DELIVERY_BOUNDARY

    assert "禁止" in DELIVERY_BOUNDARY
    assert "send_message_by_ai" in DELIVERY_BOUNDARY
    assert "直发" in DELIVERY_BOUNDARY
    assert "需主人决策" not in DELIVERY_BOUNDARY
    assert "发起人确认" in DELIVERY_BOUNDARY


def test_group_broadcast_is_not_directed_at_event_user() -> None:
    from gsuid_core.models import Event
    from gsuid_core.ai_core.outbound import proactive_directed_target

    ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g1",
        user_id="100000002",
        WS_BOT_ID="ws1",
    )
    fill = "群通知：记录已完成"
    assert proactive_directed_target(ev, fill) == ""
    assert proactive_directed_target(ev, "@100000001 图给你了") == "100000001"
    assert proactive_directed_target(ev, "@user-42 图给你了") == "user-42"
    assert proactive_directed_target(ev, "@the 图给你了") == ""
    private = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="direct",
        group_id=None,
        user_id="100000002",
        WS_BOT_ID="ws1",
    )
    assert proactive_directed_target(private, fill) == "100000002"


def test_ooc_blocks_framework_tool_leak() -> None:
    from gsuid_core.ai_core.output_firewall import check_ooc

    hit = check_ooc(
        '唔…没法直接发图给你，这个信息图句柄是 res_abc123def，你那边用 send_message_by_ai(image_id="") 发就行。'
    )
    assert hit is not None
    assert hit.category == "system_term"
    assert any("框架泄漏" in m or "send_message" in m.lower() for m in hit.matched) or any(
        "框架泄漏" in m for m in hit.matched
    )


def test_ooc_blocks_relay_meta_speech() -> None:
    from gsuid_core.ai_core.output_firewall import check_ooc

    hit = check_ooc(
        "…唔，图渲好了…交给主人格发吧。\n\n"
        "artifact: （白云机场深度研报信息图）\n"
        "要点：目标价 9.00-10.10，预期涨幅 +18%~+32%。\n呼，好困"
    )
    assert hit is not None


def test_sanitize_relay_spoken_strips_meta() -> None:
    from gsuid_core.ai_core.planning.kanban_executor import _sanitize_relay_spoken

    out = _sanitize_relay_spoken(
        "…唔，图渲好了…交给主人格发吧。\n\n"
        "artifact: res_373793dbf002（白云机场深度研报信息图）\n"
        "要点：目标价 9.00-10.10。\n呼，好困"
    )
    assert "send_message_by_ai" not in out
    assert "主人格" not in out
    assert "res_" not in out
    assert "artifact" not in out.lower()
    assert out  # 非空角色兜底或清洗后短句


def test_interactive_and_deferred_flags_independent() -> None:
    """interactive 静默与 deferred 回灌是两套登记；超时才会 mark_deferred。"""
    from gsuid_core.ai_core.planning import kanban_executor as ke

    rid = "root_flags_indep_001"
    ke.discard_interactive_relay_root(rid)
    ke.mark_interactive_relay_root(rid)
    assert rid in ke._INTERACTIVE_RELAY_ROOTS
    assert rid not in ke._DEFERRED_MAIN_DELIVERY_ROOTS
    ke.mark_deferred_main_delivery(rid)
    assert rid in ke._DEFERRED_MAIN_DELIVERY_ROOTS
    assert ke._consume_deferred_main_delivery(rid) is True
    assert ke._consume_interactive_relay(rid) is True
    ke.discard_interactive_relay_root(rid)


def test_delivery_ledger_unique_constraint_present() -> None:
    from sqlalchemy import UniqueConstraint

    from gsuid_core.ai_core.database.outbound import DeliveryLedger

    args = DeliveryLedger.__table_args__
    constraints = [a for a in args if isinstance(a, UniqueConstraint)]
    assert constraints
    assert constraints[0].name == "ux_deliveryledger_group_res"
    assert hasattr(DeliveryLedger, "release")


def test_image_placeholder_keeps_topic_and_handle(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    from gsuid_core.models import Event
    from gsuid_core.ai_core.utils import prepare_content_payload
    from gsuid_core.ai_core.outbound import (
        QuoteResolve,
        topic_from_extra,
        remember_outbound_topic,
        format_outbound_image_placeholder,
    )
    from gsuid_core.ai_core.agent_run.state import RunOnceState
    from gsuid_core.ai_core.followup_window import (
        _states,
        note_hard_trigger,
        in_followup_window,
        revoke_fresh_hard_trigger,
    )
    from gsuid_core.ai_core.agent_run.settle import _has_unread_attachment

    assert format_outbound_image_placeholder("对照图A", "res_e82bab86168f").startswith("[图片·")
    assert "res_e82bab86168f" in format_outbound_image_placeholder("对照图A", "res_e82bab86168f")
    assert format_outbound_image_placeholder("", "") == "[图片]"

    extra: dict[str, object] = {}
    remember_outbound_topic(
        extra,
        "[框架·任务完成] 根任务: a416b1f81f84\n\n任务#1「珠海横琴+澳门三日游攻略与花费估算」已结束",
    )
    assert topic_from_extra(extra) == "珠海横琴+澳门三日游攻略"

    extra_at: dict[str, object] = {}
    remember_outbound_topic(extra_at, "@3230820675 珠海行程图给你画好了")
    assert topic_from_extra(extra_at) == "珠海行程图给你画好了"

    # 纯图引用：展开引用段落与归属行（换行分隔），且不触发「未读新附图」纠偏
    ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g1",
        user_id="2281353706",
        WS_BOT_ID="ws1",
        is_tome=True,
        reply="",
        reply_id="998877",
        text="我超",
        raw_text="我超",
        image_id_list=["img_quoted01"],
    )
    quote_line = "（被引用对象：你 23:25 发给 3230820675 的「珠海横琴+澳门三日游攻略」，句柄 res_36de5e7e129f）"
    own_line = "（你刚把「珠海横琴+澳门三日游攻略」发给了 3230820675；本条来自别的群友，不是刚才那位）"

    async def _fake_resolve(_ev: Event) -> QuoteResolve:
        return QuoteResolve(
            line=quote_line,
            topic="珠海横琴+澳门三日游攻略",
            handle="res_36de5e7e129f",
            target="3230820675",
        )

    async def _fake_ownership(_ev: Event) -> str:
        return own_line

    monkeypatch.setattr("gsuid_core.ai_core.outbound.resolve_quote", _fake_resolve)
    monkeypatch.setattr("gsuid_core.ai_core.outbound.ownership_hint", _fake_ownership)
    monkeypatch.setattr(
        "gsuid_core.ai_core.configs.ai_config.ai_config.get_config",
        lambda _k: SimpleNamespace(data=False),
    )
    msgs, _ = asyncio.run(prepare_content_payload(ev, quoted_tome=True))
    body = str(msgs[0])
    assert f"--- 引用消息 ---\n[引用图片消息]\n{quote_line}\n{own_line}\n" in body
    assert "--- 被引用消息图片ID: img_quoted01 ---" in body

    st = RunOnceState(
        user_message=msgs,
        bot=None,
        ev=ev,
        rag_context="",
        tools=[],
        return_mode="by_bot",
        output_type=None,
        intent="闲聊",
        has_active_task=False,
        budget_gate=False,
        suppress_intermediate_text=False,
        fake_done_retry=False,
        turn_graph=None,
        cheap_gate=None,
        is_framework_injection=False,
    )
    assert not _has_unread_attachment(st, video_readable=False)

    # 引用触发若最终 <SILENCE>：撤销刚开启的单次续聊窗，已有多轮互动的窗保留
    _states.clear()
    note_hard_trigger("s_quote", "u_bystander", 300, 900)
    assert in_followup_window("s_quote", "u_bystander", 300, 900)
    revoke_fresh_hard_trigger("s_quote", "u_bystander")
    assert not in_followup_window("s_quote", "u_bystander", 300, 900)

    note_hard_trigger("s_multi", "u_active", 300, 900)
    _states["s_multi::u_active"].burst_start -= 10.0
    revoke_fresh_hard_trigger("s_multi", "u_active")
    assert in_followup_window("s_multi", "u_active", 300, 900)
    _states.clear()
