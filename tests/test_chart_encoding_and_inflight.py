"""图表编码 + 在途静默：结构判据回归（无业务域词表）。"""

from __future__ import annotations

import re

from gsuid_core.ai_core.agent_run.speech_policy import (
    spoken_user_body,
    spoken_user_body_len,
    looks_like_report_speech,
    looks_like_numeric_recitation,
    looks_like_task_accept_speech,
    should_block_user_visible_text,
    looks_like_inflight_quota_speech,
)
from gsuid_core.ai_core.buildin_tools.chart_svg import chart_spec_to_svg


def test_grouped_bar_has_legend_and_full_labels() -> None:
    svg = chart_spec_to_svg(
        {
            "type": "bar",
            "signed": True,
            "title": "阶段对比",
            "series": [
                {
                    "name": "对象甲",
                    "data": [
                        {"label": "近30日", "value": 2.99},
                        {"label": "近3月", "value": -4.20},
                    ],
                },
                {
                    "name": "对象乙",
                    "data": [
                        {"label": "近30日", "value": 1.76},
                        {"label": "近3月", "value": -1.51},
                    ],
                },
            ],
        }
    )
    assert svg.startswith("<svg")
    assert "对象甲" in svg and "对象乙" in svg
    assert "近30日" in svg and "近3月" in svg
    assert svg.count("<rect") >= 4


def test_chart_spec_unwraps_provider_item_wrapper() -> None:
    svg = chart_spec_to_svg(
        {
            "type": "line",
            "title": "趋势",
            "series": [
                {
                    "name": "高温",
                    "data": {
                        "item": [
                            {"label": "d1", "value": 31},
                            {"label": "d2", "value": 32},
                            {"label": "d3", "value": 30},
                        ]
                    },
                }
            ],
        }
    )
    assert svg.startswith("<svg")
    assert "⚠️" not in svg
    assert "d1" in svg and "d3" in svg


def test_hbar_keeps_long_category_label() -> None:
    label = "第一组百分位读数"
    svg = chart_spec_to_svg(
        {
            "type": "hbar",
            "data": [
                {"label": label, "value": 13.32},
                {"label": "第二组", "value": 3.01},
            ],
        }
    )
    assert "第一组百分位读数" in svg or "第一组百分位读数…" in svg
    assert "百分位" in svg


def test_unsigned_series_does_not_use_sign_palette_for_identity() -> None:
    svg = chart_spec_to_svg(
        {
            "type": "bar",
            "signed": False,
            "series": [
                {"name": "左列", "data": [{"label": "A", "value": 3}, {"label": "B", "value": 5}]},
                {"name": "右列", "data": [{"label": "A", "value": 4}, {"label": "B", "value": 2}]},
            ],
        }
    )
    assert "左列" in svg and "右列" in svg
    assert "#38bdf8" in svg
    assert "#f59e0b" in svg


def test_spoken_body_strips_ascii_wake_prefix() -> None:
    blob = (
        "[用户发言]\n[⚡主人] Someone(用户ID:1)\n--- 消息 ---\n"
        "alpha你还在吗\n[当前时间：2026-08-16 20:25:28]\n（口吻：迷糊）"
    )
    assert spoken_user_body(blob) == "你还在吗"
    assert spoken_user_body_len(blob) == 4
    assert spoken_user_body("早上好") == "早上好"
    assert spoken_user_body("Hello there") == "Hello there"
    assert spoken_user_body("OK thanks") == "OK thanks"


def test_signed_bars_use_zero_baseline() -> None:
    svg = chart_spec_to_svg(
        {
            "type": "bar",
            "signed": True,
            "data": [
                {"label": "上", "value": 4},
                {"label": "下", "value": -4},
            ],
        }
    )
    rects = re.findall(
        r'<rect x="([^"]+)" y="([^"]+)" width="([^"]+)" height="([^"]+)"',
        svg,
    )
    bars = [(float(y), float(h)) for _, y, _, h in rects if float(h) > 12]
    assert len(bars) >= 2
    assert bars[0][0] < bars[1][0]


def test_multi_line_keeps_series_legend() -> None:
    svg = chart_spec_to_svg(
        {
            "type": "line",
            "series": [
                {"name": "左列", "data": [{"label": "t1", "value": 1}, {"label": "t2", "value": 3}]},
                {"name": "右列", "data": [{"label": "t1", "value": 2}, {"label": "t2", "value": 4}]},
            ],
        }
    )
    assert "左列" in svg and "右列" in svg
    assert svg.count("<path") >= 2


def test_numeric_recitation_is_structural() -> None:
    dump = (
        "报告写好了… 读数甲 6804.46、读数乙 12658.53… 分位 13~15%、3~5% "
        "半年 -1.95%，另一项 -64.15%、出口 -46.6% 技术面未金叉，均线已下破…"
        "图还在赶，马上好"
    )
    assert looks_like_numeric_recitation(dump)
    assert looks_like_report_speech(dump)
    assert not looks_like_numeric_recitation("知道啊…就是你嘛…催了三次了…呼…")
    assert not looks_like_numeric_recitation("有。第四大股东还拿着 4.38，呼。")


def test_inflight_and_active_task_block_numeric_dump() -> None:
    dump = (
        "报告写好了… 读数甲 6804.46、读数乙 12658.53… 分位 13~15%、3~5% "
        "半年 -1.95%，另一项 -64.15%、出口 -46.6% 技术面未金叉，均线已下破…"
        "图还在赶，马上好"
    )
    blk, why = should_block_user_visible_text(
        "framework_deliver",
        dump,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        render_inflight=True,
    )
    assert blk and why == "silence_only_or_async"

    blk2, why2 = should_block_user_visible_text(
        "free",
        dump,
        pending_async=False,
        image_sent=False,
        has_status_tool=True,
        tool_calls_so_far=["check_delegation"],
        has_active_task=True,
    )
    assert blk2 and why2 == "numeric_recitation"

    blk3, _ = should_block_user_visible_text(
        "free",
        "知道啊…就是你嘛…呼…",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["search_cognition"],
        has_active_task=True,
    )
    assert not blk3


def test_delivery_copy_forbids_expanding_long_text() -> None:
    from typing import Any

    from gsuid_core.ai_core.planning.kanban_executor import _format_delivery_for_main_agent

    class _Task:
        ordinal = 1
        display_name = "单元测交付"
        failure_reason: str | None = None

    class _Art:
        id = "res_abc123456789"
        mime = "text/markdown"
        summary = "摘要一行即可"
        payload_path = "/tmp/x.md"
        payload_inline: str | None = None

    task: Any = _Task()
    arts: Any = [_Art()]
    text = _format_delivery_for_main_agent(task, "A" * 50_000, arts)
    assert "禁止为写台词去展开长文" in text
    assert "禁止把事实包数字念成群聊台词" in text
    assert "A" * 100 not in text
    assert "limit=8000" not in text
    assert "limit=8000" not in text


def test_render_prompt_mentions_series_encoding() -> None:
    from gsuid_core.ai_core.capability_agents.profiles import _RENDER_PROMPT, _RESEARCH_PROMPT
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        POST_TOOL_OUTPUT_CONTRACT_RENDER,
    )

    assert "series" in _RENDER_PROMPT
    assert "signed" in _RENDER_PROMPT
    assert "拍扁" in _RENDER_PROMPT
    assert "series" in POST_TOOL_OUTPUT_CONTRACT_RENDER
    assert "source + as_of" in _RESEARCH_PROMPT
    assert "a~b" in _RESEARCH_PROMPT


def test_unsigned_mixed_signs_keep_series_colors() -> None:
    svg = chart_spec_to_svg(
        {
            "type": "bar",
            "signed": False,
            "series": [
                {"name": "左列", "data": [{"label": "A", "value": 3}, {"label": "B", "value": -2}]},
                {"name": "右列", "data": [{"label": "A", "value": 4}, {"label": "B", "value": 1}]},
            ],
        }
    )
    assert "#38bdf8" in svg
    assert "#f59e0b" in svg
    assert "左列" in svg and "右列" in svg


def test_sparse_line_does_not_impute_zero() -> None:
    svg = chart_spec_to_svg(
        {
            "type": "line",
            "series": [
                {
                    "name": "左列",
                    "data": [{"label": "t1", "value": 8}, {"label": "t3", "value": 8}],
                },
                {
                    "name": "右列",
                    "data": [{"label": "t1", "value": 2}, {"label": "t2", "value": 4}, {"label": "t3", "value": 6}],
                },
            ],
        }
    )
    # 左列缺 t2：只画断点圆，不得把缺失连成跌到 0 的折线。
    assert 'stroke="#38bdf8"' not in svg
    assert 'stroke="#f59e0b"' in svg


def test_plus_signed_strings_parse() -> None:
    svg = chart_spec_to_svg({"type": "bar", "data": [{"label": "A", "value": "+2.99"}]})
    assert svg.startswith("<svg")
    assert "2.99" in svg


def test_category_label_keeps_eighteen_chars() -> None:
    label = "一二三四五六七八九十一二三四五六七八"
    svg = chart_spec_to_svg({"type": "hbar", "data": [{"label": label, "value": 3}]})
    assert "一二三四五六七八九十一二三四五六七八" in svg


def test_clock_fields_are_not_numeric_recitation() -> None:
    blob = "还在弄，刚才 2026-08-16 20:25:28 那轮已经进后台了，你再等一小会儿就好，别催。"
    assert not looks_like_numeric_recitation(blob)


def test_status_ok_with_tool_allows_progress() -> None:
    blob = (
        "进度 12.51%、33.12%、8.04%、21.60%、4.18%、9.03% 还在刷，"
        "刚才那轮已经进后台了，你再等一小会儿就好，别把数字当台词念出来啊。"
        "我去查过看板了。"
    )
    assert looks_like_numeric_recitation(blob)
    blk, why = should_block_user_visible_text(
        "status_ok",
        blob,
        pending_async=False,
        image_sent=False,
        has_status_tool=True,
        tool_calls_so_far=["list_my_kanban_tasks"],
        has_active_task=True,
    )
    assert not blk, why


def test_task_accept_speech_not_capped_at_twelve() -> None:
    """开场接任务应可以超过 12 字；在途第二句仍只一次。"""
    accept = "唔…四个城市六年的对照曲线…好麻烦…"
    assert len(accept) > 12
    assert looks_like_task_accept_speech(accept)
    blk, why = should_block_user_visible_text(
        "silence_only",
        accept,
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        wait_comfort_sent=False,
        speech_len_hard=150,
    )
    assert not blk, why
    blk2, why2 = should_block_user_visible_text(
        "silence_only",
        accept,
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        wait_comfort_sent=True,
        speech_len_hard=150,
    )
    assert blk2 and why2 == "silence_only_or_async"


def test_inflight_quota_allows_short_ack_once() -> None:
    ack = "唔…弄好发你。"
    assert looks_like_inflight_quota_speech(ack)
    blk, why = should_block_user_visible_text(
        "silence_only",
        ack,
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        wait_comfort_sent=False,
    )
    assert not blk, why
    blk2, why2 = should_block_user_visible_text(
        "silence_only",
        ack,
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        wait_comfort_sent=True,
    )
    assert blk2 and why2 == "silence_only_or_async"


def test_inflight_quota_rejects_wait_plus_list() -> None:
    dump = (
        "唔…图还在渲，先给你看要点：\n\n"
        "**对象甲**\n芯片 · 内存 · 屏幕刷新 · 续航 · 重量 · 防护\n\n"
        "**对象乙**\n另一套规格 · 镜头 · 电池 · 重量 · 防护\n"
        "两边差不多，你自己挑。"
    )
    assert not looks_like_inflight_quota_speech(dump)
    blk, why = should_block_user_visible_text(
        "silence_only",
        dump,
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        wait_comfort_sent=False,
        render_inflight=True,
    )
    assert blk and why == "silence_only_or_async"
