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


def test_sayu_persona_analysis_must_delegate() -> None:
    from gsuid_core.ai_core.persona.prompts import sayu_persona_prompt

    assert "委派" in sayu_persona_prompt or "影分身" in sayu_persona_prompt
    assert "公猫" in sayu_persona_prompt or "不是猫" in sayu_persona_prompt
    assert "吱一声" not in sayu_persona_prompt or "禁止引导" in sayu_persona_prompt


def test_research_prompt_has_depth_checklist() -> None:
    from gsuid_core.ai_core.capability_agents.profiles import _RESEARCH_PROMPT

    assert "时间轴" in _RESEARCH_PROMPT
    assert "web_fetch" in _RESEARCH_PROMPT
    assert "artifact_put" in _RESEARCH_PROMPT
    assert "web_search" in _RESEARCH_PROMPT
    assert "现价" in _RESEARCH_PROMPT or "市价" in _RESEARCH_PROMPT


def test_web_search_results_frame_stale_prices() -> None:
    from gsuid_core.ai_core.buildin_tools.web_search import _format_results_for_model

    text = _format_results_for_model([{"title": "gold", "url": "https://example.com", "content": "XAU 3000"}])
    assert "过时" in text or "滞后" in text
    assert "专域" in text or "API" in text
    assert "<search_results>" in text


def test_render_long_md_default_off() -> None:
    from gsuid_core.ai_core.configs.ai_config import AI_CONFIG

    cfg = AI_CONFIG["render_long_markdown_as_image"]
    assert cfg.data is False


def test_post_tool_still_domain_free() -> None:
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        POST_TOOL_OUTPUT_CONTRACT,
    )

    assert "render_agent" in POST_TOOL_OUTPUT_CONTRACT
    assert "股票" not in POST_TOOL_OUTPUT_CONTRACT
