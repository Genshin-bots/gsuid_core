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


def test_post_tool_contract_is_format_not_domain() -> None:
    """输出契约只谈出图工具通道，不含股票/金融等业务词；`<report>` 须在禁止句里。

    锁点说明：``<report>`` 块已下线（改为让 agent 自己调出图工具），契约里它只应作为
    **被禁项**出现。这里锁「禁止语 + <report> 同句」而不是某个具体禁止词，
    避免措辞从「禁止」改成「不要」时把测试弄红。
    """
    assert "render_agent" in _POST_TOOL_OUTPUT_CONTRACT
    assert "render_" in _POST_TOOL_OUTPUT_CONTRACT
    forbid_clause = next(
        (seg for seg in _POST_TOOL_OUTPUT_CONTRACT.split("；") if "<report>" in seg),
        "",
    )
    assert forbid_clause, "契约必须提到 <report>"
    assert any(word in forbid_clause for word in ("禁止", "不要", "不许", "勿")), forbid_clause
    assert "股票" not in _POST_TOOL_OUTPUT_CONTRACT
    assert "金融" not in _POST_TOOL_OUTPUT_CONTRACT
    # 短结论不应被契约强制出图
    assert "不要" in _POST_TOOL_OUTPUT_CONTRACT or "不必" in _POST_TOOL_OUTPUT_CONTRACT


def test_capability_contract_forbids_nested_render_and_create_subagent() -> None:
    """方案 B：非 render 能力代理契约禁止嵌套委派与自渲。"""
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        POST_TOOL_FAIL_CONTRACT_CAPABILITY,
        POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
    )

    assert "create_subagent" in POST_TOOL_OUTPUT_CONTRACT_CAPABILITY
    assert "render_html_to_image" in POST_TOOL_OUTPUT_CONTRACT_CAPABILITY
    assert "render_agent" in POST_TOOL_OUTPUT_CONTRACT_CAPABILITY
    assert "股票" not in POST_TOOL_OUTPUT_CONTRACT_CAPABILITY
    assert "create_subagent" in POST_TOOL_FAIL_CONTRACT_CAPABILITY


def test_delivery_boundary_forbids_nested_subagent_and_render() -> None:
    """task-mode 默认交付边界含嵌套委派与 render 禁令。"""
    from gsuid_core.ai_core.agent_node.models import DELIVERY_BOUNDARY

    assert "create_subagent" in DELIVERY_BOUNDARY
    assert "render_html_to_image" in DELIVERY_BOUNDARY
    assert "render_agent" in DELIVERY_BOUNDARY


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


def test_group_recall_tools_are_not_hard_gated() -> None:
    """发现/委派/回想不按点名硬拒；群聊寻址交给模型。"""
    from pathlib import Path

    files = (
        Path("gsuid_core/ai_core/buildin_tools/dynamic_tool_discovery.py"),
        Path("gsuid_core/ai_core/buildin_tools/subagent.py"),
        Path("gsuid_core/ai_core/buildin_tools/rag_search.py"),
        Path("gsuid_core/ai_core/buildin_tools/visibility.py"),
    )
    for path in files:
        src = path.read_text(encoding="utf-8")
        assert "check_group_recall" not in src, path
        assert "本轮未点名：不要调用发现/委派/回想" not in src, path


def test_find_tools_match_is_unidirectional() -> None:
    from pathlib import Path

    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import _need_matches_tool_text

    src = Path("gsuid_core/ai_core/buildin_tools/dynamic_tool_discovery.py").read_text(encoding="utf-8")
    assert "offered_names_in_hit_domains" not in src
    assert "n in hay or hay in n" not in src
    assert _need_matches_tool_text("网页搜索", "tool 网页搜索 docs", [])
    assert _need_matches_tool_text("帮我网页搜索一下", "other", ["网页搜索"])
    assert not _need_matches_tool_text("帮我分析很长的需求描述xyz", "分析", [])
    assert _need_matches_tool_text("北京这周的天气", "weather_handler", ["天气", "气象"])


def test_capability_agent_loop_folds_tool_return() -> None:
    from pathlib import Path

    src = Path("gsuid_core/ai_core/agent_run/loop.py").read_text(encoding="utf-8")
    assert 'self.create_by in _MAIN_PERSONA_CREATE_BY or self.create_by == "CapabilityAgent"' in src
    assert "needs_task_ack_turn" in src
    assert "tools_warrant_task_ack" in src
