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


def test_render_html_tool_is_media_category() -> None:
    """避免走 buildin_tools 包 __init__（会拉 skills 依赖）；直接读源码注解。"""
    from pathlib import Path

    src = Path("gsuid_core/ai_core/buildin_tools/html_render_tools.py").read_text(encoding="utf-8")
    # 注册装饰器：render_html_to_image 必须是 media，不能再 buildin 保底
    assert '@ai_tools(category="media", capability_domain="资料出图")' in src
    assert "async def render_html_to_image" in src
    # 文档约定委派
    assert "render_agent" in src


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
    assert "send_message_by_ai" in RENDER_DONE_RECEIPT_MARK


def test_builtin_nodes_use_registered_date_tool_name() -> None:
    _ensure_builtin_profiles()
    from gsuid_core.ai_core.agent_node import list_nodes

    for node in list_nodes():
        if node.source != "builtin":
            continue
        assert "get_current_date" not in node.tool_names, node.node_id
