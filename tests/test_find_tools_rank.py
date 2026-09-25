"""find_tools 分档：专用能力 > 专用工具 > 通用能力 > 通用工具。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.output_firewall import EXPOSED_TOOLS_EXTRA_KEY
from gsuid_core.ai_core.persona.prompts import TOOL_ORCHESTRATION_CONSTRAINTS
from gsuid_core.ai_core.buildin_tools.find_tools_rank import (
    RankedHit,
    agent_is_dedicated,
    build_find_tools_plan,
    classify_callable_tool,
    format_find_tools_plan,
    need_matches_node_text,
    need_matches_tool_text,
    covers_from_trigger_keywords,
)


def _hit(name: str, label: str = "", score: float = 1.0) -> RankedHit:
    return RankedHit(name=name, label=label or name, score=score)


def test_need_matches_node_text_rejects_unrelated_hay() -> None:
    stock_when = "分析个股指数板块期货的实时行情量价估值财务"
    assert not need_matches_node_text(
        "查询指定城市的天气和气温",
        when_to_use=stock_when,
        display_name="个股分析",
        keywords=("查询", "行情", "分析"),
        covers=("个股", "指数"),
    )
    assert need_matches_node_text(
        "查询鸣潮矩阵叠兵个人战绩",
        when_to_use="查询用户本人在全息矩阵的挑战记录",
        display_name="矩阵叠兵",
        keywords=("矩阵叠兵",),
        covers=("矩阵叠兵",),
    )


def test_need_match_unidirectional_and_cjk_window() -> None:
    assert need_matches_tool_text("网页搜索", "tool 网页搜索 docs", [])
    assert need_matches_tool_text("帮我网页搜索一下", "other", ["网页搜索"])
    assert not need_matches_tool_text("帮我分析很长的需求描述xyz", "分析", [])
    assert need_matches_tool_text("帮我查北京天气", "other", ["北京天气"])
    assert not need_matches_tool_text(
        "查询鸣潮面板的详情面板图并生成面板图片",
        "get_pool_countdown 卡池倒计时",
        ["面板"],
    )
    hay = "查询用户本人在鸣潮「全息矩阵」（矩阵叠兵 / 终焉矩阵）的挑战记录"
    assert need_matches_tool_text("查询鸣潮矩阵叠兵个人战绩记录和分数", hay, [])
    assert not need_matches_tool_text("查询鸣潮矩阵叠兵个人战绩记录和分数", "用户记忆与好感度", [])


def test_covers_from_trigger_keywords_skips_abbrev() -> None:
    out = covers_from_trigger_keywords(
        ("矩阵", "终焉矩阵", "矩阵叠兵", "jz", "sy"),
        ["已有覆盖"],
    )
    assert out[0] == "已有覆盖"
    assert "矩阵叠兵" in out
    assert "终焉矩阵" in out
    assert "jz" not in out
    assert "sy" not in out
    assert "矩阵" not in out


def test_classify_plugin_trigger_is_dedicated_core_is_generic() -> None:
    assert (
        classify_callable_tool(
            name="send_waves_matrix_info",
            category="by_trigger",
            plugin="XutheringWavesUID",
            hide_from_main=False,
            exclusive=set(),
        )
        == "dedicated"
    )
    assert (
        classify_callable_tool(
            name="search_cognition",
            category="buildin",
            plugin="core",
            hide_from_main=False,
            exclusive=set(),
        )
        == "generic"
    )
    assert (
        classify_callable_tool(
            name="render_html_to_image",
            category="media",
            plugin="core",
            hide_from_main=False,
            exclusive=set(),
        )
        == "fold"
    )
    assert (
        classify_callable_tool(
            name="render_chart_spec",
            category="common",
            plugin="core",
            hide_from_main=False,
            exclusive={"render_chart_spec"},
        )
        == "fold"
    )


def test_research_agent_is_generic_render_is_dedicated() -> None:
    assert not agent_is_dedicated("research_agent")
    assert not agent_is_dedicated("memory_curator")
    assert not agent_is_dedicated("internal_reporter")
    assert not agent_is_dedicated("scheduler_assistant")
    assert agent_is_dedicated("render_agent")
    assert agent_is_dedicated("code_agent")
    assert agent_is_dedicated("rh_aigc_agent")


def test_dedicated_tool_drops_generic_agents() -> None:
    plan = build_find_tools_plan(
        dedicated_agents=[],
        dedicated_tools=[_hit("send_waves_matrix_info", "矩阵叠兵挑战记录")],
        generic_agents=[
            _hit("research_agent"),
            _hit("memory_curator"),
            _hit("internal_reporter"),
        ],
        generic_tools=[_hit("web_search_tool")],
    )
    assert plan.has_dedicated()
    assert [h.name for h in plan.dedicated_tools] == ["send_waves_matrix_info"]
    assert plan.generic_agents == ()
    assert plan.generic_tools == ()
    assert plan.loadable_tool_names() == ["send_waves_matrix_info"]
    text = format_find_tools_plan(plan)
    assert "send_waves_matrix_info" in text
    assert "不要 create_subagent" in text
    assert "research_agent" not in text
    assert "有专用项时禁止选通用项" in text


def test_exclusive_fold_agent_outranks_plugin_tool_in_format_order() -> None:
    plan = build_find_tools_plan(
        dedicated_agents=[_hit("render_agent", "把已有事实包渲成图片")],
        dedicated_tools=[_hit("send_waves_matrix_info", "矩阵叠兵")],
        generic_agents=[_hit("research_agent")],
        generic_tools=[],
    )
    text = format_find_tools_plan(plan)
    assert text.index("【专用能力】") < text.index("【专用工具】")
    assert "`render_agent`" in text
    assert "render_html_to_image" not in text
    assert "research_agent" not in text


def test_generic_only_when_no_dedicated() -> None:
    plan = build_find_tools_plan(
        dedicated_agents=[],
        dedicated_tools=[],
        generic_agents=[_hit("research_agent", "多步外部资料收集")],
        generic_tools=[_hit("web_search_tool", "网页搜索")],
    )
    assert not plan.has_dedicated()
    assert plan.loadable_tool_names() == ["web_search_tool"]
    text = format_find_tools_plan(plan)
    assert "【通用能力】" in text
    assert "research_agent" in text
    assert "有专用项时禁止选通用项" not in text


def test_orchestration_prompt_follows_tier_order() -> None:
    assert "专用能力>专用工具>通用能力>通用工具" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "对口专用项禁止改用通用项" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "有专用工具直接调勿改派" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "优先 subagent" not in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "优先 create_subagent" not in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "零调用禁止说做不到" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "列表没有对口工具先 `find_tools`" in TOOL_ORCHESTRATION_CONSTRAINTS


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.max_retries = 1

    async def prepare_tool_def(self, _ctx: object) -> object:
        return self


def _matrix_tb() -> SimpleNamespace:
    return SimpleNamespace(
        name="send_waves_matrix_info",
        category="by_trigger",
        plugin="XutheringWavesUID",
        hide_from_main=False,
        covers=["矩阵叠兵"],
        description="查询用户本人在鸣潮全息矩阵的挑战记录",
        retrieval_text="send_waves_matrix_info 查询用户本人在鸣潮「全息矩阵」（矩阵叠兵）的挑战记录",
    )


def test_find_tools_matrix_need_loads_plugin_not_research() -> None:
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import find_tools

    tb = _matrix_tb()
    tool = _FakeTool("send_waves_matrix_info")

    async def fake_search(**_kwargs: object) -> list[_FakeTool]:
        return [tool]

    async def fake_matched(_need: str, *, limit: int = 5) -> list[str]:
        return ["research_agent", "memory_curator", "internal_reporter", "scheduler_assistant"]

    def fake_find(name: str) -> SimpleNamespace | None:
        if name == "send_waves_matrix_info":
            return tb
        return None

    ctx = MagicMock()
    ctx.deps = ToolContext(
        extra={EXPOSED_TOOLS_EXTRA_KEY: ["find_tools", "search_cognition"]},
        blocked_tool_names=set(),
    )

    async def _run() -> str:
        with (
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.search_tools_by_domain",
                fake_search,
            ),
            patch("gsuid_core.ai_core.register.find_tool_base", fake_find),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._matched_capability_node_ids",
                fake_matched,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.nodes_with_keyword_hit",
                lambda _n: ["research_agent"],
            ),
        ):
            return await find_tools(ctx, "查询鸣潮矩阵叠兵个人战绩记录和分数")

    out = asyncio.run(_run())
    assert "send_waves_matrix_info" in out
    assert "【专用工具】" in out
    assert "research_agent" not in out
    assert "memory_curator" not in out
    assert "有专用项时禁止选通用项" in out
    assert "send_waves_matrix_info" in ctx.deps.dynamic_tool_names


def _countdown_tb() -> SimpleNamespace:
    return SimpleNamespace(
        name="get_pool_countdown",
        category="by_trigger",
        plugin="PluginA",
        hide_from_main=False,
        covers=["面板"],
        description="卡池倒计时",
        retrieval_text="get_pool_countdown 卡池倒计时",
    )


def _char_detail_tb() -> SimpleNamespace:
    return SimpleNamespace(
        name="get_user_wuwa_char_detail",
        category="by_trigger",
        plugin="PluginA",
        hide_from_main=False,
        covers=["详情面板"],
        description="角色详情",
        retrieval_text="get_user_wuwa_char_detail 角色详情面板图",
    )


def test_find_tools_still_searches_when_short_cover_already_offered() -> None:
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import find_tools

    countdown = _countdown_tb()
    panel = _char_detail_tb()
    tool = _FakeTool("get_user_wuwa_char_detail")
    search_calls: list[int] = []

    async def fake_search(**_kwargs: object) -> list[_FakeTool]:
        search_calls.append(1)
        return [tool]

    async def fake_matched(_need: str, *, limit: int = 5) -> list[str]:
        return ["research_agent"]

    def fake_find(name: str) -> SimpleNamespace | None:
        if name == "get_pool_countdown":
            return countdown
        if name == "get_user_wuwa_char_detail":
            return panel
        return None

    ctx = MagicMock()
    ctx.deps = ToolContext(
        extra={EXPOSED_TOOLS_EXTRA_KEY: ["get_pool_countdown", "find_tools"]},
        blocked_tool_names=set(),
    )

    async def _run() -> str:
        with (
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.search_tools_by_domain",
                fake_search,
            ),
            patch("gsuid_core.ai_core.register.find_tool_base", fake_find),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._matched_capability_node_ids",
                fake_matched,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.nodes_with_keyword_hit",
                lambda _n: [],
            ),
        ):
            return await find_tools(ctx, "查询角色面板的详情面板图并生成面板图片")

    out = asyncio.run(_run())
    assert search_calls == [1]
    assert "get_user_wuwa_char_detail" in out
    assert "当前列表已有对应能力族工具" not in out
    assert "get_pool_countdown" not in out
    assert "research_agent" not in out
    assert "get_user_wuwa_char_detail" in ctx.deps.dynamic_tool_names


def test_find_tools_merges_already_offered_true_hit() -> None:
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import find_tools

    panel = _char_detail_tb()
    search_calls: list[int] = []

    async def fake_search(**_kwargs: object) -> list[_FakeTool]:
        search_calls.append(1)
        return []

    async def fake_visible(_ctx: object, names: object) -> list[str]:
        return list(names) if isinstance(names, list) else []

    async def fake_matched(_need: str, *, limit: int = 5) -> list[str]:
        return ["research_agent"]

    def fake_find(name: str) -> SimpleNamespace | None:
        if name == "get_user_wuwa_char_detail":
            return panel
        return None

    ctx = MagicMock()
    ctx.deps = ToolContext(
        extra={EXPOSED_TOOLS_EXTRA_KEY: ["get_user_wuwa_char_detail"]},
        blocked_tool_names=set(),
    )

    async def _run() -> str:
        with (
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.search_tools_by_domain",
                fake_search,
            ),
            patch("gsuid_core.ai_core.register.find_tool_base", fake_find),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.visible_offered_names",
                fake_visible,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._matched_capability_node_ids",
                fake_matched,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.nodes_with_keyword_hit",
                lambda _n: [],
            ),
        ):
            return await find_tools(ctx, "查询角色详情面板")

    out = asyncio.run(_run())
    assert search_calls == [1]
    assert "get_user_wuwa_char_detail" in out
    assert "未检索到" not in out
    assert "【专用工具】" in out
    assert "research_agent" not in out


def test_find_tools_folds_exclusive_render_to_agent() -> None:
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import find_tools

    tb = SimpleNamespace(
        name="render_html_to_image",
        category="media",
        plugin="core",
        hide_from_main=False,
        covers=["HTML出图"],
        description="把 HTML 渲成图",
        retrieval_text="render_html_to_image 把 HTML 渲成图片",
    )
    tool = _FakeTool("render_html_to_image")

    async def fake_search(**_kwargs: object) -> list[_FakeTool]:
        return [tool]

    async def fake_matched(_need: str, *, limit: int = 5) -> list[str]:
        return ["research_agent"]

    def fake_find(name: str) -> SimpleNamespace | None:
        if name == "render_html_to_image":
            return tb
        return None

    def fake_owning(_names: list[str]) -> dict[str, list[str]]:
        return {"render_html_to_image": ["render_agent"]}

    def fake_node_hit(node_id: str, score: float) -> RankedHit | None:
        if node_id == "render_agent":
            return RankedHit(name="render_agent", label="把已有事实包渲成图片", score=score)
        return None

    ctx = MagicMock()
    ctx.deps = ToolContext(extra={EXPOSED_TOOLS_EXTRA_KEY: []}, blocked_tool_names={"render_html_to_image"})

    async def _run() -> str:
        with (
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.search_tools_by_domain",
                fake_search,
            ),
            patch("gsuid_core.ai_core.register.find_tool_base", fake_find),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._matched_capability_node_ids",
                fake_matched,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.nodes_with_keyword_hit",
                lambda _n: [],
            ),
            patch(
                "gsuid_core.ai_core.agent_node.registry.owning_nodes_of_tools",
                fake_owning,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._node_hit",
                fake_node_hit,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._node_on_topic",
                lambda _need, nid: nid == "render_agent",
            ),
        ):
            return await find_tools(ctx, "把这份对比表出成图")

    out = asyncio.run(_run())
    assert "`render_agent`" in out
    assert "【专用能力】" in out
    assert "render_html_to_image" not in out
    assert "research_agent" not in out
    assert "render_html_to_image" not in ctx.deps.dynamic_tool_names


def test_find_tools_unrelated_keyword_node_keeps_generic() -> None:
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import find_tools

    async def fake_search(**_kwargs: object) -> list[_FakeTool]:
        return []

    async def fake_matched(_need: str, *, limit: int = 5) -> list[str]:
        return ["research_agent"]

    def fake_node_hit(node_id: str, score: float) -> RankedHit | None:
        return RankedHit(name=node_id, label=node_id, score=score)

    ctx = MagicMock()
    ctx.deps = ToolContext(extra={EXPOSED_TOOLS_EXTRA_KEY: []}, blocked_tool_names=set())

    async def _run() -> str:
        with (
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.search_tools_by_domain",
                fake_search,
            ),
            patch("gsuid_core.ai_core.register.find_tool_base", lambda _n: None),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._matched_capability_node_ids",
                fake_matched,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.nodes_with_keyword_hit",
                lambda _n: ["stock_agent"],
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._node_on_topic",
                lambda _need, _nid: False,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._node_hit",
                fake_node_hit,
            ),
        ):
            return await find_tools(ctx, "查询指定城市的天气和气温")

    out = asyncio.run(_run())
    assert "stock_agent" not in out
    assert "research_agent" in out
    assert "有专用项时禁止选通用项" not in out
    assert "做不到" not in out


def test_find_tools_unrelated_exclusive_fold_keeps_generic() -> None:
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import find_tools

    tb = SimpleNamespace(
        name="render_html_to_image",
        category="media",
        plugin="core",
        hide_from_main=False,
        covers=["HTML出图"],
        description="把 HTML 渲成图",
        retrieval_text="render_html_to_image 把 HTML 渲成图片",
    )
    tool = _FakeTool("render_html_to_image")
    render_node = SimpleNamespace(
        node_id="render_agent",
        when_to_use="把已有事实包/对比表/指标/清单渲成美观图片",
        display_name="视觉渲染",
        match_keywords=("出图", "对比表", "信息图"),
    )

    async def fake_search(**_kwargs: object) -> list[_FakeTool]:
        return [tool]

    async def fake_matched(_need: str, *, limit: int = 5) -> list[str]:
        return ["research_agent"]

    def fake_find(name: str) -> SimpleNamespace | None:
        if name == "render_html_to_image":
            return tb
        return None

    def fake_owning(_names: list[str]) -> dict[str, list[str]]:
        return {"render_html_to_image": ["render_agent"]}

    def fake_get_node(nid: str) -> SimpleNamespace | None:
        if nid == "render_agent":
            return render_node
        return None

    def fake_node_hit(node_id: str, score: float) -> RankedHit | None:
        if node_id == "render_agent":
            return RankedHit(name="render_agent", label="把已有事实包渲成图片", score=score)
        if node_id == "research_agent":
            return RankedHit(name="research_agent", label="多步外部资料收集", score=score)
        return None

    ctx = MagicMock()
    ctx.deps = ToolContext(extra={EXPOSED_TOOLS_EXTRA_KEY: []}, blocked_tool_names={"render_html_to_image"})

    async def _run() -> str:
        with (
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.search_tools_by_domain",
                fake_search,
            ),
            patch("gsuid_core.ai_core.register.find_tool_base", fake_find),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._matched_capability_node_ids",
                fake_matched,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery.nodes_with_keyword_hit",
                lambda _n: [],
            ),
            patch(
                "gsuid_core.ai_core.agent_node.registry.owning_nodes_of_tools",
                fake_owning,
            ),
            patch(
                "gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery._node_hit",
                fake_node_hit,
            ),
            patch("gsuid_core.ai_core.agent_node.get_node", fake_get_node),
            patch(
                "gsuid_core.ai_core.agent_node.semantic_routing.aggregate_node_covers",
                lambda _n: ["出图", "对比表"],
            ),
        ):
            return await find_tools(ctx, "查询指定城市的天气和气温")

    out = asyncio.run(_run())
    assert "render_agent" not in out
    assert "research_agent" in out
    assert "有专用项时禁止选通用项" not in out


def test_find_tools_gap_note_does_not_tell_model_to_give_up() -> None:
    from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import FIND_TOOLS_GAP_NOTE

    assert "做不到" not in FIND_TOOLS_GAP_NOTE
    assert "capability_map" not in FIND_TOOLS_GAP_NOTE
    assert "禁止对用户讲检索过程" in FIND_TOOLS_GAP_NOTE
