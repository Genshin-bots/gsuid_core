"""主人格话术策略：单表面 / 进度追问 / 可出图候选（结构判据）。"""

from __future__ import annotations

from gsuid_core.ai_core.agent_run.speech_policy import (
    IN_FLIGHT_WAIT_TEMPLATES,
    wall_clock_nudge_for,
    resolve_speech_policy,
    looks_like_process_meta,
    looks_like_empty_handoff,
    looks_like_wait_template,
    claims_premature_delivery,
    looks_like_status_inquiry,
    content_is_render_candidate,
    has_orchestration_narration,
    should_mark_speech_delivered,
    should_block_user_visible_text,
    looks_like_inflight_quota_speech,
)


def test_status_inquiry_detects_progress_questions() -> None:
    assert looks_like_status_inquiry("图好了吗", has_active_task=True)
    assert looks_like_status_inquiry("还要多久啊", has_active_task=False)
    assert looks_like_status_inquiry("呢", has_active_task=True)
    assert not looks_like_status_inquiry("呢", has_active_task=False)
    assert not looks_like_status_inquiry("早上好", has_active_task=True)


def test_status_inquiry_strips_assembled_shell() -> None:
    blob = (
        "[用户发言]\n[⚡主人] 我\n--- 消息 ---\n图呢\n"
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
    assert has_orchestration_narration("让帮手去查一下")
    assert not has_orchestration_narration("让我去看看")
    assert not has_orchestration_narration("我自己去办")


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
        "马上好。",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert not blk2, why2
    blk2b, why2b = should_block_user_visible_text(
        "silence_only",
        "马上好。",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
        wait_comfort_sent=True,
    )
    assert blk2b and why2b == "silence_only_or_async"

    blk3, _ = should_block_user_visible_text(
        "framework_nudge",
        "zzz…没啥好画的…别折腾我…",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert blk3

    # 进度追问：零工具不得报进度（含极短完成句）
    blk4, why4 = should_block_user_visible_text(
        "status_ok",
        "应该快好了吧，你再等一下应该就行了",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert blk4 and why4 == "status_without_tool"
    blk4s, why4s = should_block_user_visible_text(
        "status_ok",
        "做完了…zzz",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
    )
    assert blk4s and why4s == "status_without_tool"

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


def test_process_meta_and_empty_handoff_gates() -> None:
    assert looks_like_process_meta("…时效存疑，自己再验。")
    assert looks_like_process_meta("唔…数据没刷出来，没法给你编数字。")
    assert looks_like_process_meta("…先眯会儿，回炉了你再戳我。")
    assert not looks_like_process_meta("…没查到具体数字。…困。")
    assert looks_like_process_meta(
        "The sub-agent is running in the background. I should not narrate the process to the user."
    )
    assert not looks_like_process_meta("https://wiki.biligame.com/ys/some-long-page-name-here")

    # 无事实包：诚实失败允许（不再误武装 render）
    honest = "唔…翻了好几页，具体数字没翻到。…好困。"
    blk, why = should_block_user_visible_text(
        "free",
        honest,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["web_search_tool"],
        fact_pack_pending=False,
    )
    assert not blk, why

    # 过程元话语始终拦
    blk2, why2 = should_block_user_visible_text(
        "free",
        "…时效存疑，自己再验。",
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["web_search_tool"],
        fact_pack_pending=False,
    )
    assert blk2 and why2 == "process_meta"

    # 有事实包 + 摆烂句才 empty_handoff
    lazy = "卷轴里都记着呢，要哪段再喊我。"
    assert looks_like_empty_handoff(lazy)
    blk3, why3 = should_block_user_visible_text(
        "free",
        lazy,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["web_search_tool"],
        fact_pack_pending=True,
    )
    assert blk3 and why3 == "empty_handoff"
    blk4, _ = should_block_user_visible_text(
        "free",
        lazy,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["web_search_tool"],
        fact_pack_pending=False,
    )
    assert not blk4


def test_wall_clock_pipeline_branch() -> None:
    close = wall_clock_nudge_for(need_render_pipeline=False)
    pipe = wall_clock_nudge_for(need_render_pipeline=True)
    assert "不要再发起新的工具调用" in close or "不要再发起任何新的工具调用" in close
    assert "render_agent" in pipe
    assert "SILENCE" in pipe
    # 有事实包时必须硬开 render 例外
    assert ("禁止" in pipe and "停工具" in pipe) or ("硬例外" in pipe)


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

    # 报告体只在**真有待出图事实包**时才拦（出处凭据）；
    # 无事实包的长正文是用户点名要的（作文/代码/翻译），见控制面 INV-1。
    blk, why = should_block_user_visible_text(
        "free",
        typhoon,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["web_search_tool"],
        fact_pack_pending=True,
    )
    assert blk and why == "report_speech"

    blk_no_pack, _ = should_block_user_visible_text(
        "free",
        typhoon,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
        fact_pack_pending=False,
    )
    assert not blk_no_pack


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
    assert blk and why in ("empty_handoff", "premature_delivery")

    wait = "马上好。"
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


def test_wait_comfort_does_not_mark_delivered() -> None:
    assert not should_mark_speech_delivered(text="马上好。", has_media=False)
    assert not should_mark_speech_delivered(text="这就去办", has_media=False)
    assert should_mark_speech_delivered(text="查到了，出门带伞。", has_media=False)
    assert should_mark_speech_delivered(text="出门带伞", has_media=True)
    assert not should_mark_speech_delivered(text="", has_media=True)


def test_wait_templates_are_legal_inflight_exit() -> None:
    for s in IN_FLIGHT_WAIT_TEMPLATES:
        assert looks_like_wait_template(s)
        assert looks_like_inflight_quota_speech(s)
        blk, why = should_block_user_visible_text(
            "silence_only",
            s,
            pending_async=True,
            image_sent=False,
            has_status_tool=False,
            tool_calls_so_far=["create_subagent"],
            wait_comfort_sent=False,
        )
        assert not blk, why
    improv = "唔…等一下嘛"
    assert looks_like_inflight_quota_speech(improv)
    assert not looks_like_inflight_quota_speech("唔…图还在渲…呼，再眯一小会儿就好")


def test_first_ack_with_tools_keeps_accept_speech() -> None:
    from gsuid_core.ai_core.agent_run.loop import decide_text_outbound_slot

    assert decide_text_outbound_slot(has_fn_tool=True, tool_bearing_index=1, accept_slot_used=False) == "send_accept"
    assert decide_text_outbound_slot(has_fn_tool=True, tool_bearing_index=1, accept_slot_used=True) == "unsent"
    assert decide_text_outbound_slot(has_fn_tool=True, tool_bearing_index=2, accept_slot_used=False) == "unsent"
    assert decide_text_outbound_slot(has_fn_tool=False, tool_bearing_index=0, accept_slot_used=False) == "send_final"
    blk, why = should_block_user_visible_text(
        "silence_only",
        "…等数据回来再继续…",
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        wait_comfort_sent=True,
    )
    assert blk and why == "silence_only_or_async"


def test_function_tool_detected_even_if_text_part_comes_first() -> None:
    from pydantic_ai.messages import TextPart, ToolCallPart, NativeToolCallPart

    from gsuid_core.ai_core.agent_run.loop import _response_has_function_tool_call

    thinking = TextPart(content="让我先查一下再决定怎么回。")
    for name in ("find_tools", "web_search_tool", "read_handle", "send_message_by_ai"):
        call = ToolCallPart(tool_name=name, args="{}")
        assert _response_has_function_tool_call([thinking, call]) is True
        assert _response_has_function_tool_call([call, thinking]) is True
    assert _response_has_function_tool_call([thinking]) is False
    assert _response_has_function_tool_call([thinking, NativeToolCallPart(tool_name="web_search", args="{}")]) is False


def test_long_task_wait_announce_allowed() -> None:
    """步骤 3：委派前「会比较久」声明应放行（含 async/silence）。"""
    from gsuid_core.ai_core.agent_run.speech_policy import looks_like_wait_comfort

    wait = "嗯，在弄了。"
    assert looks_like_wait_comfort(wait)
    assert not should_block_user_visible_text(
        "free",
        wait,
        pending_async=False,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=[],
        wait_comfort_sent=False,
    )[0]
    assert not should_block_user_visible_text(
        "silence_only",
        wait,
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        wait_comfort_sent=False,
    )[0]


def test_post_image_caption_blocks_open_solicit() -> None:
    """发图配引导追问：与 TextPart 同一套 open_solicit 闸；角色短句仍放行。"""
    from gsuid_core.ai_core.agent_run.speech_policy import has_open_solicitation

    wrap = "弄好了你自己看吧，细节都在图上。接下来如果还需要其他分析或别的对照，请告诉我一声就行。"
    assert len(wrap) > 40
    assert has_open_solicitation(wrap)
    blk, why = should_block_user_visible_text(
        "framework_deliver",
        wrap,
        pending_async=False,
        image_sent=True,
        has_status_tool=False,
        tool_calls_so_far=["send_message_by_ai"],
    )
    assert blk and why == "open_solicit"
    close = "呼…弄好了…你看…"
    assert not should_block_user_visible_text(
        "silence_only",
        close,
        pending_async=False,
        image_sent=True,
        has_status_tool=False,
        tool_calls_so_far=["send_message_by_ai"],
    )[0]


def test_post_image_closing_speech_allowed() -> None:
    """步骤 7：发图后短收尾应放行；长结构仍拦。"""
    close = "呼…弄好了…你看…"
    # 完成腔在未发图时拦，发图后放行
    assert claims_premature_delivery(close)
    assert not should_block_user_visible_text(
        "silence_only",
        close,
        pending_async=False,
        image_sent=True,
        has_status_tool=False,
        tool_calls_so_far=["send_message_by_ai"],
    )[0]
    # 发图后仍拦长结构刷屏
    long_report = (
        "**第一节**\n" + "细节很多。\n\n" + "**第二节**\n" + "还有一堆。\n\n" + "**第三节**\n" + "继续写。" * 20
    )
    blk, why = should_block_user_visible_text(
        "free",
        long_report,
        pending_async=False,
        image_sent=True,
        has_status_tool=False,
        tool_calls_so_far=["send_message_by_ai"],
    )
    assert blk and why in ("report_speech", "post_image_too_long")


def test_async_blocks_non_wait_until_image() -> None:
    """子任务在途：非等待句应静默。"""
    blk, why = should_block_user_visible_text(
        "silence_only",
        "我先去睡觉了你自己看吧",
        pending_async=True,
        image_sent=False,
        has_status_tool=False,
        tool_calls_so_far=["create_subagent"],
        wait_comfort_sent=True,
    )
    assert blk and why == "silence_only_or_async"
