"""thrash 跨轮计数 + 能力代理过程句检测回归。"""

from __future__ import annotations


def test_thrash_same_response_parallel_counts_one_turn() -> None:
    from gsuid_core.ai_core.gs_agent import _update_thrash_streak_for_response

    # 同响应 4 次 web_search → 只 +1 轮
    name, streak = _update_thrash_streak_for_response(
        ["web_search_tool"] * 4,
        prev_name="",
        prev_streak=0,
    )
    assert name == "web_search_tool"
    assert streak == 1

    name2, streak2 = _update_thrash_streak_for_response(
        ["web_search_tool"] * 3,
        prev_name=name,
        prev_streak=streak,
    )
    assert name2 == "web_search_tool"
    assert streak2 == 2


def test_thrash_mixed_tools_resets() -> None:
    from gsuid_core.ai_core.gs_agent import _update_thrash_streak_for_response

    name, streak = _update_thrash_streak_for_response(
        ["web_search_tool", "web_fetch_tool"],
        prev_name="web_search_tool",
        prev_streak=3,
    )
    assert name == ""
    assert streak == 0


def test_thrash_empty_response_keeps_streak() -> None:
    from gsuid_core.ai_core.gs_agent import _update_thrash_streak_for_response

    name, streak = _update_thrash_streak_for_response(
        [],
        prev_name="web_search_tool",
        prev_streak=2,
    )
    assert name == "web_search_tool"
    assert streak == 2


def test_thrash_limit_is_four() -> None:
    from gsuid_core.ai_core.gs_agent import _THRASH_SAME_TOOL_LIMIT

    assert _THRASH_SAME_TOOL_LIMIT == 4


def test_post_tool_contracts_split_persona_vs_capability() -> None:
    from gsuid_core.ai_core.gs_agent import (
        _POST_TOOL_OUTPUT_CONTRACT,
        _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY,
        _post_tool_contracts_for,
    )

    ok, fail = _post_tool_contracts_for("Chat")
    assert ok is _POST_TOOL_OUTPUT_CONTRACT
    assert "render_agent" in ok
    assert "render_" in ok or "render_agent" in ok

    ok_c, fail_c = _post_tool_contracts_for("CapabilityAgent")
    assert ok_c is _POST_TOOL_OUTPUT_CONTRACT_CAPABILITY
    assert "事实包" in ok_c or "Markdown" in ok_c
    assert "禁止" in ok_c and "render_html" in ok_c
    assert "render_html_to_image" not in fail_c or "禁止" in fail_c


def test_incomplete_delivery_detects_process_only() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import (
        looks_like_incomplete_subagent_delivery,
    )

    assert looks_like_incomplete_subagent_delivery(
        "收到，停止重复调用。下面再做几次差异化的关键搜索补全本周事件，然后渲染HTML周报图。"
    )
    assert looks_like_incomplete_subagent_delivery("")
    assert looks_like_incomplete_subagent_delivery(
        "【research_agent 临时代理已完成 / transient 模式】（**未在看板创建任务卡**——lookup 模式。）"
        "主人格：角色短句结论 + 数据用 render_html_to_image 出图，禁止整段念出。\n\n"
        "收到，停止重复调用。下面再做几次。"
    )


def test_incomplete_delivery_accepts_fact_package() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import (
        looks_like_incomplete_subagent_delivery,
    )

    md = """# 金融周报 2026-07-27~08-03

## 条目
1. **2026-07-29 FOMC** 维持利率 3.50-3.75%。来源：federalreserve.gov
2. **2026-07-30 政治局会议** 部署下半年经济。来源：新华社
3. **2026-07-31 中国 PMI** 制造业 49.2%。来源：国家统计局

## 依据
- web_search_tool / get_latest_news
"""
    assert not looks_like_incomplete_subagent_delivery(md)


def test_incomplete_delivery_accepts_res_handle_summary() -> None:
    """artifact 短摘要含 res_ 不得判 incomplete（交付误杀回归）。"""
    from gsuid_core.ai_core.buildin_tools.subagent import (
        looks_like_incomplete_subagent_delivery,
    )

    summary = (
        "事实包已登记为 **`res_fa2c9a5b1364`**（22,282 字节，text/markdown）。请主persona把句柄转给 render_agent。"
    )
    assert not looks_like_incomplete_subagent_delivery(summary)
    assert not looks_like_incomplete_subagent_delivery(
        "【research_agent 临时代理已完成 / transient 模式】\n\n" + summary
    )


def test_ooc_scrub_kills_res_handle_but_capability_path_must_not() -> None:
    """roleplay scrub 会杀 res_；能力代理 return 不得走该路径。"""
    from gsuid_core.ai_core.output_firewall import check_ooc, scrub_or_fallback

    sample = "事实包已登记为 **`res_fa2c9a5b1364`**，请转 render_agent。"
    hit = check_ooc(sample)
    assert hit is not None
    out, scrubbed = scrub_or_fallback(sample)
    assert scrubbed is True
    assert "res_" not in out


def test_followup_task_mentions_no_render() -> None:
    from gsuid_core.ai_core.buildin_tools.subagent import _delivery_followup_task

    t = _delivery_followup_task("整理近一周金融新闻")
    assert "事实包" in t
    assert "render_" in t
    assert "整理近一周" in t
