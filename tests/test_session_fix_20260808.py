"""2026-08-08 会话问题修复烟测：OOC 交付 / 委派契约 / 代码查询规范化。"""

from __future__ import annotations


def test_capability_return_skips_roleplay_ooc_condition() -> None:
    """子代理/能力代理应跳过 roleplay OOC（逻辑条件与 gs_agent 对齐）。"""

    def skip(create_by: str, is_subagent: bool) -> bool:
        return is_subagent or create_by in ("CapabilityAgent", "AutoPlanner")

    assert skip("CapabilityAgent", True) is True
    assert skip("AutoPlanner", True) is True
    assert skip("Chat", False) is False
    assert skip("Chat", True) is True


def test_incomplete_with_artifact_markers() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import (
        looks_like_incomplete_subagent_delivery,
    )

    assert looks_like_incomplete_subagent_delivery("下面再搜一下然后渲染") is True
    assert looks_like_incomplete_subagent_delivery("res_aabbccddee12 已交付") is False
    assert looks_like_incomplete_subagent_delivery("已登记 artifact: res_112233445566") is False


def test_tool_orchestration_has_delegation_first() -> None:
    from gsuid_core.ai_core.persona.prompts import (
        SYSTEM_CONSTRAINTS,
        TOOL_ORCHESTRATION_CONSTRAINTS,
    )

    assert "DELEGATION_FIRST" in TOOL_ORCHESTRATION_CONSTRAINTS
    assert "重任务" in SYSTEM_CONSTRAINTS or "委派" in SYSTEM_CONSTRAINTS
    assert "禁止" in SYSTEM_CONSTRAINTS and "工具名" in SYSTEM_CONSTRAINTS
    # 长任务仍建议先等一句再委派，但不再写成硬七步
    assert "等待" in SYSTEM_CONSTRAINTS
    assert "等待" in TOOL_ORCHESTRATION_CONSTRAINTS


def test_sayu_persona_analysis_must_delegate() -> None:
    from gsuid_core.ai_core.persona.prompts import sayu_persona_prompt

    assert "委派" in sayu_persona_prompt or "得等一会儿" in sayu_persona_prompt
    assert "公猫" in sayu_persona_prompt or "不是猫" in sayu_persona_prompt
    assert "吱一声" not in sayu_persona_prompt or "禁止引导" in sayu_persona_prompt


def test_research_prompt_has_depth_checklist() -> None:
    from gsuid_core.ai_core.capability_agents.profiles import _RESEARCH_PROMPT

    assert "时间轴" in _RESEARCH_PROMPT
    assert "web_fetch" in _RESEARCH_PROMPT
    assert "artifact_put" in _RESEARCH_PROMPT
    assert "web_search" in _RESEARCH_PROMPT
    # 通用时效表述，禁止把业务域词写进契约
    assert "当前" in _RESEARCH_PROMPT or "时点" in _RESEARCH_PROMPT
    assert "结构化数据" in _RESEARCH_PROMPT or "数据工具" in _RESEARCH_PROMPT
    assert "专域" not in _RESEARCH_PROMPT
    assert "研报" not in _RESEARCH_PROMPT
    assert "股票" not in _RESEARCH_PROMPT
    assert "现价" not in _RESEARCH_PROMPT
    assert "市价" not in _RESEARCH_PROMPT
    assert "时效存疑" not in _RESEARCH_PROMPT


def test_research_match_keywords_domain_free() -> None:
    from gsuid_core.ai_core.agent_node import get_node
    from gsuid_core.ai_core.capability_agents.profiles import register_builtin_profiles

    register_builtin_profiles()
    node = get_node("research_agent")
    assert node is not None
    kws = set(node.match_keywords or [])
    assert "深渊" not in kws
    assert "调研" in kws or "分析" in kws


def test_web_search_results_frame_stale_prices() -> None:
    from gsuid_core.ai_core.buildin_tools.web_search import _format_results_for_model

    text = _format_results_for_model(
        [{"title": "gold", "url": "https://example.com", "content": "XAU 3000"}],
        query="gold",
    )
    assert "过时" in text or "滞后" in text
    assert "结构化数据" in text or "实时读数" in text
    assert "<search_results>" in text
    assert "query: gold" in text
    assert "市价" not in text
    assert "股票" not in text
    assert "时效存疑" not in text
    assert "专域" not in text


def test_render_long_md_default_off() -> None:
    from gsuid_core.ai_core.configs.ai_config import AI_CONFIG

    cfg = AI_CONFIG["render_long_markdown_as_image"]
    assert cfg.data is False


def test_post_tool_still_domain_free() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        POST_TOOL_OUTPUT_CONTRACT,
        POST_TOOL_OUTPUT_CONTRACT_RENDER,
    )

    assert "render_agent" in POST_TOOL_OUTPUT_CONTRACT
    assert "股票" not in POST_TOOL_OUTPUT_CONTRACT
    assert "游戏" not in POST_TOOL_OUTPUT_CONTRACT_RENDER
    assert "财经" not in POST_TOOL_OUTPUT_CONTRACT_RENDER
    assert "天气" not in POST_TOOL_OUTPUT_CONTRACT_RENDER
