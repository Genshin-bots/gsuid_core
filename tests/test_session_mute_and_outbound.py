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


def test_image_placeholder_keeps_topic_and_handle() -> None:
    from gsuid_core.ai_core.outbound import format_outbound_image_placeholder

    assert format_outbound_image_placeholder("对照图A", "res_e82bab86168f").startswith("[图片·")
    assert "res_e82bab86168f" in format_outbound_image_placeholder("对照图A", "res_e82bab86168f")
    assert format_outbound_image_placeholder("", "") == "[图片]"
