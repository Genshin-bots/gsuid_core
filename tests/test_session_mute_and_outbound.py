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
