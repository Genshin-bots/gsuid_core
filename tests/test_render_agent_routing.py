"""render_agent 注册 / 路由 / exclusive / 契约 / 第二跳出图闸。"""

from __future__ import annotations


def _ensure_builtin_profiles() -> None:
    from gsuid_core.ai_core.capability_agents.profiles import register_builtin_profiles

    register_builtin_profiles()


def test_render_agent_registered() -> None:
    _ensure_builtin_profiles()
    from gsuid_core.ai_core.agent_node import get_node

    node = get_node("render_agent")
    assert node is not None
    assert node.display_name == "视觉渲染"
    assert "render_html_to_image" in node.tool_names
    assert "render_card" in node.tool_names
    assert "_get_current_date" in node.tool_names
    assert "get_current_date" not in node.tool_names
    assert node.tool_packs == []
    assert "render_html" in node.prompt or "render_html_to_image" in node.prompt
    assert "web_search" in node.boundary_override or "禁止" in node.boundary_override


def test_internal_reporter_has_no_render_tools() -> None:
    """与 CAPABILITY 契约一致：internal_reporter 只交事实包，不自渲。"""
    _ensure_builtin_profiles()
    from gsuid_core.ai_core.agent_node import get_node

    node = get_node("internal_reporter")
    assert node is not None
    for name in ("render_html_to_image", "render_card", "render_markdown_to_image"):
        assert name not in node.tool_names
    assert "render_*" in node.prompt or "禁止" in node.prompt and "render" in node.prompt


def test_resolve_node_prefers_render_for_out_tu() -> None:
    _ensure_builtin_profiles()
    from gsuid_core.ai_core.agent_node import resolve_node

    assert resolve_node("帮我出图做对比表") == "render_agent"
    assert resolve_node("render_agent") == "render_agent"
    assert resolve_node("research_agent") == "research_agent"


def test_resolve_node_longest_keyword_wins_over_registration_order() -> None:
    """「分析」命中 research，但「对比表」更长 → render（修复抢占）。"""
    _ensure_builtin_profiles()
    from gsuid_core.ai_core.agent_node import resolve_node

    assert resolve_node("分析并出对比表") == "render_agent"
    assert resolve_node("帮我分析一下") == "research_agent"
    assert resolve_node("出图") == "render_agent"
    assert resolve_node("资料调研") == "research_agent"


def test_render_tools_owned_by_render_agent_not_task_basics() -> None:
    """render_* 在 render_agent 白名单，且不在 task_basics 共享包（可被 exclusive 剥离）。"""
    _ensure_builtin_profiles()
    from gsuid_core.ai_core.agent_node import (
        TASK_BASICS_PACK,
        get_node,
        resolve_pack_tool_names,
    )

    node = get_node("render_agent")
    assert node is not None
    shared = set(resolve_pack_tool_names([TASK_BASICS_PACK]))
    owned = set(node.tool_names)
    for name in ("render_html_to_image", "render_card", "render_markdown_to_image"):
        assert name in owned
        assert name not in shared


def test_compose_task_prompt_render_boundary() -> None:
    from gsuid_core.ai_core.agent_node import get_node, compose_task_prompt

    _ensure_builtin_profiles()
    node = get_node("render_agent")
    assert node is not None
    prompt = compose_task_prompt(node)
    assert "render_html_to_image" in prompt or "渲成图片" in prompt
    assert "send_message_by_ai" in prompt
    assert "禁止" in prompt


def test_research_prompt_has_freshness_and_no_render() -> None:
    _ensure_builtin_profiles()
    from gsuid_core.ai_core.agent_node import get_node

    node = get_node("research_agent")
    assert node is not None
    assert "时效" in node.prompt
    assert "禁止" in node.prompt and "render" in node.prompt
    # 不依赖其它 node_id 交叉引用
    assert "render_agent" not in node.prompt


def test_post_tool_contracts_prefer_capability_node_id() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        POST_TOOL_FAIL_CONTRACT_RENDER,
        POST_TOOL_OUTPUT_CONTRACT_RENDER,
        POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
        post_tool_contracts_for,
        is_render_capability_agent,
    )

    ok, fail = post_tool_contracts_for(
        "CapabilityAgent",
        capability_node_id="render_agent",
        session_id="capagent_research_agent_adhoc",  # 故意误导
    )
    assert ok is POST_TOOL_OUTPUT_CONTRACT_RENDER
    assert fail is POST_TOOL_FAIL_CONTRACT_RENDER

    ok2, _ = post_tool_contracts_for(
        "CapabilityAgent",
        capability_node_id="research_agent",
        session_id="capagent_render_agent_spoof",  # node_id 优先
    )
    assert ok2 is POST_TOOL_OUTPUT_CONTRACT_CAPABILITY

    assert is_render_capability_agent(session_id="capagent_render_agent_transient_x")
    assert not is_render_capability_agent(session_id="capagent_research_agent_x")
    # 子串误伤：suffix 里碰巧出现 render 字样，不得用裸 in 匹配
    assert not is_render_capability_agent(session_id="capagent_research_agent_note_about_capagent_render_agent")


def test_tool_call_targets_render_agent() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        tool_call_targets_render_agent,
    )

    _ensure_builtin_profiles()
    assert tool_call_targets_render_agent(
        tool_name="create_subagent",
        args={"task": "出图", "agent_profile": "render_agent"},
    )
    assert tool_call_targets_render_agent(
        tool_name="create_subagent",
        args={"task": "做对比表", "agent_profile": "出对比表"},
    )
    assert not tool_call_targets_render_agent(
        tool_name="create_subagent",
        args={"task": "查资料", "agent_profile": "research_agent"},
    )
    assert not tool_call_targets_render_agent(
        tool_name="web_search_tool",
        args={"query": "x"},
    )
    assert not tool_call_targets_render_agent(
        tool_name="create_subagent",
        args={"task": "查完交给 render_agent 出图", "agent_profile": "research_agent"},
        args_json='{"task":"查完交给 render_agent 出图","agent_profile":"research_agent"}',
    )


def test_inflight_rolls_back_on_failed_return() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        inflight_after_create_subagent_return,
    )

    pending, delegated, policy, ack = inflight_after_create_subagent_return(
        failed=True,
        async_ack=False,
        render_done=False,
        ack_seen=False,
        pending_async=True,
        delegated_render=True,
        speech_policy="silence_only",
        is_framework=False,
    )
    assert pending is False and delegated is False and ack is False
    assert policy == "free"

    pending2, delegated2, policy2, ack2 = inflight_after_create_subagent_return(
        failed=True,
        async_ack=False,
        render_done=False,
        ack_seen=True,
        pending_async=True,
        delegated_render=True,
        speech_policy="silence_only",
        is_framework=False,
    )
    assert pending2 is True and delegated2 is True and ack2 is True
    assert policy2 == "silence_only"


def test_tool_return_is_delegation_refusal() -> None:
    """派发前软拒绝剥掉「委派原问」导语后仍按前缀判；在途 HOLD 与 ack 不算。"""
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        PENDING_DELEGATION_HOLD,
        format_deferred_subagent_ack,
        tool_return_is_delegation_refusal,
    )

    assert tool_return_is_delegation_refusal(
        "（委派原问：出图）\n⚠️ 当前人格未启用能力代理 `render_agent`。可用 node_id：research_agent"
    )
    assert tool_return_is_delegation_refusal("⚠️ 未匹配到能力节点 `x`。可用 node_id：research_agent")
    assert tool_return_is_delegation_refusal("🚫 该能力代理仅主人可触发。（`code_agent`）")
    assert not tool_return_is_delegation_refusal(f"（委派原问：查）\n{PENDING_DELEGATION_HOLD}")
    assert not tool_return_is_delegation_refusal(
        f"（委派原问：查）\n{format_deferred_subagent_ack(ordinal=1, pid='render_agent', handle='dlg_x')}"
    )
    assert not tool_return_is_delegation_refusal("（委派原问：查）\n事实包已登记为 res_0123456789ab")
    assert not tool_return_is_delegation_refusal("正常的完成回执文本")
    assert not tool_return_is_delegation_refusal(None)


def test_inflight_rolls_back_on_refused_delegation() -> None:
    """未启用能力代理的软拒绝与硬失败同样回滚抢先静默；已有 ack 则不回滚。"""
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        inflight_after_create_subagent_return,
    )

    pending, delegated, policy, ack = inflight_after_create_subagent_return(
        failed=False,
        refused=True,
        async_ack=False,
        render_done=False,
        ack_seen=False,
        pending_async=True,
        delegated_render=True,
        speech_policy="silence_only",
        is_framework=True,
    )
    assert pending is False and delegated is False and ack is False
    assert policy == "framework_deliver"

    pending2, delegated2, policy2, ack2 = inflight_after_create_subagent_return(
        failed=False,
        refused=True,
        async_ack=False,
        render_done=False,
        ack_seen=True,
        pending_async=True,
        delegated_render=True,
        speech_policy="silence_only",
        is_framework=True,
    )
    assert pending2 is True and delegated2 is True and ack2 is True
    assert policy2 == "silence_only"


def test_apply_create_subagent_return_rolls_back_on_refusal() -> None:
    """loop 侧回滚：拒绝回执把抢先置位的三个标志拨回可交付态。"""
    from pydantic_ai.messages import ToolReturnPart

    from gsuid_core.ai_core.agent_run.loop import LoopPhase
    from gsuid_core.ai_core.agent_run.state import RunOnceState

    st = RunOnceState(
        user_message="",
        bot=None,
        ev=None,
        rag_context=None,
        tools=[],
        return_mode="by_bot",
        output_type=None,
        intent=None,
        has_active_task=False,
        budget_gate=False,
        suppress_intermediate_text=False,
        fake_done_retry=False,
        turn_graph=None,
        cheap_gate=None,
        is_framework_injection=True,
    )
    st.delegated_render = True
    st.pending_async_delivery = True
    st.speech_policy = "silence_only"
    st.fw_msg = True
    phase = LoopPhase.__new__(LoopPhase)
    body = "（委派原问：出图）\n⚠️ 当前人格未启用能力代理 `render_agent`。可用 node_id：research_agent"
    part = ToolReturnPart(tool_name="create_subagent", content=body, tool_call_id="t1")
    phase._apply_create_subagent_return(st, part, body)
    assert st.pending_async_delivery is False
    assert st.delegated_render is False
    assert st.speech_policy == "framework_deliver"
    assert st.saw_structured_return is False

    # deferred ack：保持静默在途
    st2 = RunOnceState(
        user_message="",
        bot=None,
        ev=None,
        rag_context=None,
        tools=[],
        return_mode="by_bot",
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
    st2.delegated_render = True
    st2.pending_async_delivery = True
    st2.speech_policy = "silence_only"
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.capability_agents.delegation_contracts import format_deferred_subagent_ack

    st2.context = ToolContext()
    ack_body = f"（委派原问：出图）\n{format_deferred_subagent_ack(ordinal=7, pid='render_agent', handle='dlg_ab')}"
    part2 = ToolReturnPart(tool_name="create_subagent", content=ack_body, tool_call_id="t2")
    phase._apply_create_subagent_return(st2, part2, ack_body)
    assert st2.pending_async_delivery is True
    assert st2.speech_policy == "silence_only"
    assert st2.render_ack_seen is True


def test_receipt_image_likely_not_any_artifact() -> None:
    """非图 artifact 不得触发「可发图」口吻。"""
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        RENDER_DONE_RECEIPT_MARK,
        receipt_image_likely,
    )

    assert not receipt_image_likely(pid="render_agent", has_image_art=False)
    assert receipt_image_likely(pid="code_agent", has_image_art=True)
    assert not receipt_image_likely(pid="research_agent", has_image_art=False)
    assert not receipt_image_likely(pid="code_agent", has_image_art=False)
    assert receipt_image_likely(pid="render_agent", has_image_art=True)
    assert "图片" in RENDER_DONE_RECEIPT_MARK or "产物" in RENDER_DONE_RECEIPT_MARK


def test_builtin_nodes_use_registered_date_tool_name() -> None:
    _ensure_builtin_profiles()
    from gsuid_core.ai_core.agent_node import list_nodes

    for node in list_nodes():
        if node.source != "builtin":
            continue
        assert "get_current_date" not in node.tool_names, node.node_id
