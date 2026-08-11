"""2026-08-11 归因修复（方案一~十）的纯函数回归测试。"""

from __future__ import annotations

# ── 方案八：chart_spec → SVG 原语 ──────────────────────────────────────────


def test_chart_line_svg_basics() -> None:
    from gsuid_core.ai_core.buildin_tools.chart_svg import chart_spec_to_svg

    svg = chart_spec_to_svg(
        {
            "type": "line",
            "data": [{"label": d, "value": v} for d, v in zip(["07-20", "07-21", "07-22"], [4002, 4077, 4130])],
        }
    )
    assert svg.startswith("<svg")
    assert "<path" in svg
    assert "4130" in svg  # 尾点数值标注
    assert "</svg>" in svg


def test_chart_bar_and_hbar() -> None:
    from gsuid_core.ai_core.buildin_tools.chart_svg import chart_spec_to_svg

    data = [{"label": "A", "value": 3}, {"label": "B", "value": 9}]
    bar = chart_spec_to_svg({"type": "bar", "data": data})
    hbar = chart_spec_to_svg({"type": "hbar", "data": data})
    assert "<rect" in bar and "</svg>" in bar
    assert "<rect" in hbar and "</svg>" in hbar


def test_chart_pie_shares_percentages() -> None:
    from gsuid_core.ai_core.buildin_tools.chart_svg import chart_spec_to_svg

    svg = chart_spec_to_svg({"type": "pie", "data": [{"label": "股", "value": 70}, {"label": "债", "value": 30}]})
    assert "<path" in svg
    assert "70%" in svg and "30%" in svg


def test_chart_handles_bad_input() -> None:
    from gsuid_core.ai_core.buildin_tools.chart_svg import chart_spec_to_svg

    assert chart_spec_to_svg({"type": "line", "data": []}).startswith("⚠️")
    assert chart_spec_to_svg({"type": "radar", "data": [{"label": "x", "value": 1}]}).startswith("⚠️")
    # 裸数值列表容忍
    svg = chart_spec_to_svg({"type": "line", "data": [1, 2, 3]})
    assert svg.startswith("<svg")


def test_chart_size_clamped() -> None:
    from gsuid_core.ai_core.buildin_tools.chart_svg import chart_spec_to_svg

    svg = chart_spec_to_svg({"type": "bar", "data": [{"label": "a", "value": 1}], "width": 99999, "height": 1})
    assert 'width="1200"' in svg  # 上限截断
    assert 'height="160"' in svg  # 下限抬升


# ── 方案九：工具健康度滑窗冻结 ────────────────────────────────────────────


def test_tool_health_freeze_after_consecutive_fails() -> None:
    from gsuid_core.ai_core import tool_health

    tool_health._HEALTH.clear()
    name = "unit_test_broken_tool"
    assert not tool_health.is_tool_frozen(name)
    tool_health.record_tool_failure(name, "boom1")
    tool_health.record_tool_failure(name, "boom2")
    assert not tool_health.is_tool_frozen(name)
    tool_health.record_tool_failure(name, "boom3")
    assert tool_health.is_tool_frozen(name)
    assert "临时停用" in tool_health.frozen_tool_message(name)
    snap = tool_health.get_tool_health_snapshot()
    assert snap[0]["name"] == name and snap[0]["frozen"] is True


def test_tool_health_success_resets_streak() -> None:
    from gsuid_core.ai_core import tool_health

    tool_health._HEALTH.clear()
    name = "unit_test_flaky_tool"
    tool_health.record_tool_failure(name)
    tool_health.record_tool_failure(name)
    tool_health.record_tool_success(name)
    tool_health.record_tool_failure(name)
    tool_health.record_tool_failure(name)
    # 连败被成功清零过，2 次连败不应冻结
    assert not tool_health.is_tool_frozen(name)


# ── 方案七：时效契约标记 ──────────────────────────────────────────────────


def test_fresh_and_web_marks() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        tool_return_has_fresh_mark,
        tool_return_is_non_web_data,
        tool_return_has_web_source_mark,
    )

    assert tool_return_has_fresh_mark("[as_of=2026-08-11 23:00|source=eastmoney-kline]\n【X】")
    assert tool_return_has_fresh_mark('{"as_of": "2026-08-11", "price": 4384}')
    assert not tool_return_has_fresh_mark("普通文本，无时点")
    assert tool_return_has_web_source_mark("[source=web|staleness_risk=high]\n<search_results>")
    assert not tool_return_has_web_source_mark("[as_of=2026-08-11]")
    assert not tool_return_has_fresh_mark(None)
    assert not tool_return_has_web_source_mark(123)
    # 非 web 成功数据 vs web / 软失败：mixed turn 不得再谎称「只有 web」
    assert tool_return_is_non_web_data("XAUUSD 现价 4384.2\n来源 eastmoney")
    assert not tool_return_is_non_web_data("[source=web|staleness_risk=high]\n旧闻")
    assert not tool_return_is_non_web_data("⚠️ 标的解析失败")
    assert not tool_return_is_non_web_data("❌ 超时")
    assert not tool_return_is_non_web_data("[]")
    # find_tools 路由/装配元返回不算实质数据（否则 find_tools→web 会挡掉 WEB_ONLY）
    assert not tool_return_is_non_web_data("🔎 未检索到可直接加载的工具，但可委派")
    assert not tool_return_is_non_web_data("🔒 该类工具为能力代理专属，请 create_subagent")
    assert not tool_return_is_non_web_data("✅ 已加载以下工具，下一步即可直接调用：\n- x")


def test_web_only_caveat_text_actionable() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import WEB_ONLY_STALENESS_CAVEAT

    # 必须给出路（结构化工具重取 / 如实说查不到），而非只禁止
    assert "结构化数据工具" in WEB_ONLY_STALENESS_CAVEAT
    assert "如实" in WEB_ONLY_STALENESS_CAVEAT


# ── 方案四：强化出图契约 + 纠正 nudge 集合 ────────────────────────────────


def test_render_required_contract_locks_next_step() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        POST_TOOL_OUTPUT_CONTRACT_RENDER_REQUIRED,
    )

    assert "唯一合法下一步" in POST_TOOL_OUTPUT_CONTRACT_RENDER_REQUIRED
    assert "render_agent" in POST_TOOL_OUTPUT_CONTRACT_RENDER_REQUIRED


def test_correction_nudge_markers_cover_all_nudges() -> None:
    from gsuid_core.ai_core.agent_run.support import (
        _FAKE_DONE_NUDGE,
        _STRUCTURAL_ZERO_TOOL_NUDGE,
        _correction_nudge_markers,
    )

    markers = _correction_nudge_markers()
    assert _FAKE_DONE_NUDGE in markers
    assert _STRUCTURAL_ZERO_TOOL_NUDGE in markers
    assert len(markers) >= 5
    assert all(isinstance(m, str) and m for m in markers)


# ── 方案二/三：委派路由与 find_tools 分流 ─────────────────────────────────


def test_owning_nodes_of_tools_maps_tool_names() -> None:
    from gsuid_core.ai_core.agent_node import register_agent_node
    from gsuid_core.ai_core.agent_node.models import AgentNode
    from gsuid_core.ai_core.agent_node.registry import owning_nodes_of_tools, unregister_agent_node

    node = AgentNode(
        node_id="unit_test_owner_node", display_name="测试节点", prompt="p", tool_names=["unit_tool_a", "unit_tool_b"]
    )
    register_agent_node(node)
    try:
        owners = owning_nodes_of_tools(["unit_tool_a", "unit_tool_x"])
        assert owners["unit_tool_a"] == ["unit_test_owner_node"]
        assert "unit_tool_x" not in owners
    finally:
        unregister_agent_node("unit_test_owner_node")


def test_semantic_routing_helpers_pure() -> None:
    from gsuid_core.ai_core.agent_node.models import AgentNode
    from gsuid_core.ai_core.agent_node.semantic_routing import (
        _cosine,
        build_node_retrieval_text,
    )

    node = AgentNode(
        node_id="stock_agent",
        display_name="股票研究分析代理",
        prompt="p",
        when_to_use="分析个股/期货/现货贵金属（黄金XAU）",
        match_keywords=["黄金"],
    )
    text = build_node_retrieval_text(node)
    assert "stock_agent" in text and "XAU" in text and "黄金" in text
    assert abs(_cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert _cosine([], [1.0]) == 0.0


def test_find_tools_no_longer_grants_answer_from_knowledge() -> None:
    """删除「据现有能力作答」许可证：失败分支文案不得再出现该短语。"""
    import inspect

    from gsuid_core.ai_core.buildin_tools import dynamic_tool_discovery

    # 检查整个模块（find_tools + 辅助函数），而不只是 ai_tools 包装层
    src = inspect.getsource(dynamic_tool_discovery)
    assert "create_subagent" in src
    code_lines = [line for line in src.splitlines() if "据现有能力作答" in line and not line.lstrip().startswith("#")]
    assert code_lines == [], f"旧文案仍出现在非注释行：{code_lines[:2]}"


# ── 方案一：covers 元数据进检索面 ─────────────────────────────────────────


def test_toolbase_retrieval_text_includes_covers_aliases() -> None:
    from typing import Any

    from gsuid_core.ai_core.models import ToolBase

    fake_tool: Any = object()
    tb = ToolBase(
        name="search_stock",
        description="搜索标的代码",
        plugin="SayuStock",
        tool=fake_tool,
        covers=["A股/期货/现货贵金属（黄金XAU）"],
        aliases=["金融·标的解析"],
    )
    text = tb.retrieval_text
    assert "search_stock" in text
    assert "XAU" in text
    assert "金融·标的解析" in text
