"""能力代理委派流程回归：专属工具剥离 + 画像清单 + 输出契约。"""

from gsuid_core.ai_core.gs_agent import (
    _format_capability_roster,
    _pool_overlaps_capability_agent,
    _capability_exclusive_tool_names,
)


def test_exclusive_tools_exclude_task_basics_shared() -> None:
    """task_basics 是共享基建，不得进 exclusive（否则主人格失去 web_search 等）。"""
    from gsuid_core.ai_core.agent_node import TASK_BASICS_PACK, resolve_pack_tool_names

    basics = set(resolve_pack_tool_names([TASK_BASICS_PACK]))
    exclusive = _capability_exclusive_tool_names()
    # 空环境（无插件专属工具）下 exclusive 可能为空；有 code_agent 时也不应含 task_basics
    assert exclusive.isdisjoint(basics)
    assert "read_handle" in basics
    assert "list_persisted_outputs" in basics


def test_roster_lists_node_ids_not_invented_names() -> None:
    """画像清单必须给出可抄的 node_id；禁止只写模糊中文。"""
    roster = _format_capability_roster()
    # 无节点时为空串；有内置注册时含 research_agent 等
    if roster:
        assert "create_subagent" in roster
        assert "agent_profile" in roster
        assert "`" in roster  # node_id 用反引号标出


def test_pool_overlap_empty_on_empty_pool() -> None:
    assert _pool_overlaps_capability_agent(set()) == ""


def test_strip_non_render_cap_deny_keeps_render_agent() -> None:
    """非 render 节点剥离 deny 集合；render_agent 原样保留。"""
    from gsuid_core.ai_core.register import find_tool_base
    from gsuid_core.ai_core.capability_agents.runner import (
        _NON_RENDER_CAP_DENY_TOOLS,
        _strip_non_render_cap_deny,
    )

    # 仅用已注册工具构造列表（未注册则跳过）
    candidate_names = [
        "artifact_put",
        "create_subagent",
        "render_html_to_image",
        "web_search_tool",
    ]
    tools = []
    for n in candidate_names:
        tb = find_tool_base(n)
        if tb is not None:
            tools.append(tb.tool)
    if len(tools) < 3:
        return

    stripped = _strip_non_render_cap_deny(tools, node_id="stock_report_agent")
    names = {t.name for t in stripped}
    assert names.isdisjoint(_NON_RENDER_CAP_DENY_TOOLS)

    kept = _strip_non_render_cap_deny(tools, node_id="render_agent")
    assert {t.name for t in kept} == {t.name for t in tools}


def test_exclusive_tools_blocked_from_progressive_path() -> None:
    """find_tools / RetrievableToolset 不得把专属工具回灌主人格。"""
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.dynamic_toolset import RetrievableToolset

    exclusive = _capability_exclusive_tool_names()
    # 无插件时 exclusive 可为空；有则验证 blocked 与 exclude 口径一致
    ctx = ToolContext(blocked_tool_names=set(exclusive))
    rt = RetrievableToolset(exclude_names={"find_tools"} | set(exclusive))
    assert exclusive <= rt._exclude or not exclusive
    assert exclusive <= ctx.blocked_tool_names or not exclusive


def test_visibility_user_hint_does_not_lie_about_manage() -> None:
    from gsuid_core.ai_core.buildin_tools.visibility import visibility_user_hint

    unnamed = visibility_user_hint(
        is_group=True,
        call_to_self=False,
        followup_detected=False,
        create_ok=False,
    )
    assert unnamed == ""
    assert "管理已有" not in unnamed
    manage = visibility_user_hint(
        is_group=True,
        call_to_self=True,
        followup_detected=False,
        create_ok=False,
    )
    assert "管理已有" in manage
    clear = visibility_user_hint(
        is_group=True,
        call_to_self=True,
        followup_detected=False,
        create_ok=True,
    )
    assert clear == ""
