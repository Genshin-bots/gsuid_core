"""主人格话术策略：单表面 / 进度追问 / 可出图候选（结构判据）。"""

from __future__ import annotations

from gsuid_core.ai_core.agent_run.speech_policy import (
    wall_clock_nudge_for,
    resolve_speech_policy,
    claims_premature_delivery,
    looks_like_status_inquiry,
    content_is_render_candidate,
    has_orchestration_narration,
    should_block_user_visible_text,
)


def test_status_inquiry_detects_progress_questions() -> None:
    assert looks_like_status_inquiry("图好了吗", has_active_task=True)
    assert looks_like_status_inquiry("还要多久啊", has_active_task=False)
    assert looks_like_status_inquiry("呢", has_active_task=True)
    assert not looks_like_status_inquiry("呢", has_active_task=False)
    assert not looks_like_status_inquiry("早上好", has_active_task=True)


def test_status_inquiry_strips_assembled_shell() -> None:
    blob = (
        "[用户发言]\n[⚡主人] 我找你说话了。\n--- 消息 ---\n图呢\n"
        "[当前时间：2026-08-08 22:00:00]\n"
        "【你正在为对方推进的事项】事项#1｜运行中"
    )
    assert looks_like_status_inquiry(blob, has_active_task=True)


def test_orchestration_and_premature_delivery() -> None:
    leak = "详情让render出了个图，你看看…我要睡了…zzz"
    assert has_orchestration_narration(leak)
    assert claims_premature_delivery(leak)
    assert claims_premature_delivery("画好了，你看")
    assert not claims_premature_delivery("唔…还在弄…再等等")


def test_speech_block_policies() -> None:
    leak = "详情让render出了个图"
    blk, why = should_block_user_visible_text(
        "free",
        leak,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert blk and why in ("orchestration_leak", "premature_delivery")

    blk2, why2 = should_block_user_visible_text(
        "silence_only",
        "唔…还在画…",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert blk2 and why2 == "silence_only_or_async"

    blk3, _ = should_block_user_visible_text(
        "framework_nudge",
        "zzz…没啥好画的…别折腾我…",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert blk3

    # 进度追问：零工具不得报进度长句
    blk4, why4 = should_block_user_visible_text(
        "status_ok",
        "应该快好了吧，你再等一下应该就行了",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert blk4 and why4 == "status_without_tool"

    # 查过工具后允许角色短句
    blk5, _ = should_block_user_visible_text(
        "status_ok",
        "唔…还在弄…再等等…",
        pending_async=False,
        image_sent=False,
        has_status_tool=True,
        tool_calls_so_far=["list_my_kanban_tasks"],
    )
    assert not blk5

    # SILENCE 永不拦
    assert not should_block_user_visible_text(
        "silence_only",
        "<SILENCE>",
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )[0]


def test_resolve_speech_policy() -> None:
    assert (
        resolve_speech_policy(
            is_framework=True,
            fake_done_retry=False,
            is_status_inquiry=False,
            has_active_task=False,
            user_text="（系统校验：本轮工具已返回",
        )
        == "framework_nudge"
    )
    assert (
        resolve_speech_policy(
            is_framework=True,
            fake_done_retry=False,
            is_status_inquiry=False,
            has_active_task=True,
            user_text="[框架·任务完成]\n子任务交付",
        )
        == "framework_deliver"
    )
    assert (
        resolve_speech_policy(
            is_framework=False,
            fake_done_retry=False,
            is_status_inquiry=True,
            has_active_task=True,
            user_text="图好了吗",
        )
        == "status_ok"
    )
    assert (
        resolve_speech_policy(
            is_framework=False,
            fake_done_retry=False,
            is_status_inquiry=False,
            has_active_task=False,
            user_text="早上好",
        )
        == "free"
    )


def test_render_candidate_not_volume_only() -> None:
    # 短噪声 / 失败：不可出图
    assert not content_is_render_candidate(
        tool_name="web_search_tool",
        content="抓取失败: 网络请求失败",
        fileos_folded=False,
    )
    assert not content_is_render_candidate(
        tool_name="find_tools",
        content="✅ 已加载以下工具，下一步即可直接调用：\n- nte_account\n",
        fileos_folded=False,
    )
    # 真表 / 事实包
    table = "| a | b |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
    assert content_is_render_candidate(
        tool_name="create_subagent",
        content="事实包如下\n" + table + "x" * 80,
        fileos_folded=False,
    )
    # 仅 FileOS 折叠短卡：默认不武装
    assert not content_is_render_candidate(
        tool_name="web_search_tool",
        content="[persisted id=to_abc kind=tool_output]\nsummary: 加载中",
        fileos_folded=True,
    )


def test_wall_clock_pipeline_branch() -> None:
    close = wall_clock_nudge_for(need_render_pipeline=False)
    pipe = wall_clock_nudge_for(need_render_pipeline=True)
    assert "不要再发起任何新的工具调用" in close
    assert "render_agent" in pipe
    assert "SILENCE" in pipe


def test_report_speech_and_solicitation() -> None:
    from gsuid_core.ai_core.agent_run.speech_policy import (
        has_open_solicitation,
        looks_like_report_speech,
        strip_open_solicitations,
    )

    typhoon = (
        "呼…大概弄清楚了…\n\n"
        "**命名规则**\n亚太14个国家和地区各起10个名字…一共140个…\n\n"
        "**近期**\n搜到白海豚…无法确认路径…\n\n"
        "要不要我换个关键词再查一次…zzz\n\n"
        "…\n\n"
        "再多说一句：命名表2000年起启用…"
    )
    assert looks_like_report_speech(typhoon)
    assert has_open_solicitation(typhoon)
    cleaned = strip_open_solicitations(typhoon)
    assert "要不要" not in cleaned
    assert "命名" in cleaned or "140" in cleaned or "白海豚" in cleaned

    blk, why = should_block_user_visible_text(
        "free",
        typhoon,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["web_search_tool"],
    )
    assert blk and why == "report_speech"


def test_empty_handoff_and_wait_comfort() -> None:
    from gsuid_core.ai_core.agent_run.speech_policy import (
        looks_like_wait_comfort,
        looks_like_empty_handoff,
    )

    lazy = "唔…翻完了…\n1.1到3.6…十六个版本…好长…念不动…\n卷轴里全记着呢…呼…\n要哪段再喊我…先睡了…"
    assert looks_like_empty_handoff(lazy)
    assert claims_premature_delivery(lazy)
    blk, why = should_block_user_visible_text(
        "free",
        lazy,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        fact_pack_pending=True,
    )
    assert blk and why in ("empty_handoff", "premature_delivery", "pre_render_long_speech")

    wait = "唔…等一下…画张图…"
    assert looks_like_wait_comfort(wait)
    assert not looks_like_empty_handoff(wait)
    assert not should_block_user_visible_text(
        "free",
        wait,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        fact_pack_pending=True,
        wait_comfort_sent=False,
    )[0]
    # 异步中只放行一次等待
    assert not should_block_user_visible_text(
        "silence_only",
        wait,
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
        wait_comfort_sent=False,
    )[0]
    assert should_block_user_visible_text(
        "silence_only",
        wait,
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
        wait_comfort_sent=True,
    )[0]


def test_persona_bubble_clamp() -> None:
    """空行拆条后超过 2 段应并入末条（逻辑与 send_chat_result 一致）。"""
    import re

    text = "a\n\nb\n\nc\n\nd\n\ne\n\nf\n\ng"
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    max_b = 2
    if len(blocks) > max_b:
        head = blocks[: max_b - 1]
        tail = "\n".join(b.strip() for b in blocks[max_b - 1 :])
        blocks = [*head, tail]
    assert len(blocks) == 2
    assert "g" in blocks[-1]
