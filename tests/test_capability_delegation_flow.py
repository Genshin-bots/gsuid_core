"""能力代理委派流程回归：专属工具剥离 + 画像清单 + 输出契约。"""

from gsuid_core.ai_core.gs_agent import (
    _POST_TOOL_OUTPUT_CONTRACT,
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


def test_post_tool_contract_is_format_not_domain() -> None:
    """输出契约只谈通道（report），不含股票/金融等业务词。"""
    assert "<report" in _POST_TOOL_OUTPUT_CONTRACT
    assert "股票" not in _POST_TOOL_OUTPUT_CONTRACT
    assert "金融" not in _POST_TOOL_OUTPUT_CONTRACT


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
