"""send_message_by_ai 的两条回归锁（对应 session ...200000001 早柚"狂飙 + 刷 markdown"，群号已脱敏）：

1. **文本走 send_chat_result**：send_message_by_ai 发文本时必须经统一归一化链
   （剥 markdown / 长文转图 / 连发拆条），不再裸 bot.send 把 ``**加粗**`` 刷进群。
2. **单轮硬限流**：同一 (session, turn) 内调用超过 PER_TURN_SEND_MESSAGE_LIMIT 直接拒发，
   返回"不是常规回复通道"的提示，把模型推回正文输出；换新回合 / 清理后额度重置。

用 asyncio.run 包装（不依赖 pytest-asyncio）。
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


def _make_ev(session_id: str = "s1", user_id: str = "u1") -> Any:
    ev = MagicMock()
    ev.session_id = session_id
    ev.user_id = user_id
    ev.group_id = "g1"
    ev.raw_text = ""
    return ev


def _make_ctx(ev: Any, turn_id: str, bot: Any) -> Any:
    from gsuid_core.ai_core.models import ToolContext

    ctx = MagicMock()
    ctx.deps = ToolContext(
        bot=bot,
        ev=ev,
        extra={"turn_id": turn_id},
        parent_session_id=None,
    )
    return ctx


def _run(coro):
    return asyncio.run(coro)


def test_text_routes_through_send_chat_result_and_not_raw_send():
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="route_s", user_id="u1")
    ms.clear_turn_send_throttle("route_s", "turn_route")

    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()) as scr,
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
    ):
        ctx = _make_ctx(ev, "turn_route", bot)
        result = _run(ms.send_message_by_ai(ctx, text="**【赢家】** 贝莱德 +8%\n- 阿里 +4%"))

    assert "消息已发送" in result
    # 文本必须经 send_chat_result（markdown 归一化在其中），且不走裸 bot.send
    assert scr.await_count == 1
    assert scr.await_args is not None
    assert scr.await_args.args[1] == "**【赢家】** 贝莱德 +8%\n- 阿里 +4%"
    assert scr.await_args.kwargs["ooc_check"] is False
    assert bot.send.await_count == 0
    print("[OK] 文本走 send_chat_result、未裸 bot.send")


def test_per_turn_throttle_rejects_third_call():
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="spam_s", user_id="u1")
    ms.clear_turn_send_throttle("spam_s", "turn_spam")

    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()) as scr,
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
    ):
        ctx = _make_ctx(ev, "turn_spam", bot)
        r1 = _run(ms.send_message_by_ai(ctx, text="第一条"))
        r2 = _run(ms.send_message_by_ai(ctx, text="第二条"))
        r3 = _run(ms.send_message_by_ai(ctx, text="第三条"))

    assert "消息已发送" in r1 and "消息已发送" in r2
    assert "不是常规回复通道" in r3
    assert scr.await_count == ms.PER_TURN_SEND_MESSAGE_LIMIT == 2
    print("[OK] 第 3 条被单轮硬限流拒发")


def test_cs_caption_kept_with_image() -> None:
    """配图时追问和较长说明随图发出。"""
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="cs_s", user_id="u1")
    ms.clear_turn_send_throttle("cs_s", "turn_cs")
    wrap = "弄好了你自己看吧，细节都在图上。接下来如果还需要其他分析或别的对照，请告诉我一声就行。"

    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()) as scr,
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
    ):
        ctx = _make_ctx(ev, "turn_cs", bot)
        result = _run(ms.send_message_by_ai(ctx, text=wrap, image_id="http://example.test/a.png"))

    assert "消息已发送" in result
    assert bot.send.await_count == 1
    assert scr.await_count == 1
    assert scr.await_args is not None
    sent_text = scr.await_args.args[1]
    assert isinstance(sent_text, str)
    assert "请告诉" in sent_text
    assert "如果还需要" in sent_text


def test_caption_with_image_is_sent() -> None:
    """多段结果说明和图一起发出，不再当成报告体丢掉字、只发图。"""
    from gsuid_core.ai_core.buildin_tools import message_sender as ms
    from gsuid_core.ai_core.agent_run.speech_policy import looks_like_report_speech

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="cap_s", user_id="uuu")
    ms.clear_turn_send_throttle("cap_s", "turn_cap")
    caption = (
        "做完啦…结果页原文是酱紫的：\n\n"
        "灵魂角色：影山茂夫（Mob）《灵能百分百》，灵魂重合度 93%。\n\n"
        "十维坐标：理性 0／秩序 1／依恋 21／向阳 45／利他 93。\n\n"
        "也很像的几位：碇真嗣 91%、夏目贵志 90%、阿尔敏 89%。\n\n"
        "唔…利他 93 分呢。图给你放在上面啦。"
    )
    assert looks_like_report_speech(caption)

    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()) as scr,
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
    ):
        ctx = _make_ctx(ev, "turn_cap", bot)
        ctx.tool_call_id = "call_caption"
        result = _run(ms.send_message_by_ai(ctx, text=caption, image_id="http://example.test/card.png"))

    assert "消息已发送" in result
    assert "未发送" not in result
    assert scr.await_count == 1
    assert scr.await_args is not None
    assert scr.await_args.args[1] == caption
    assert bot.send.await_count == 1


def test_speech_refusal_returns_reason_and_sends_nothing() -> None:
    """话术不合格时整次不发，把原因退回，让模型改写。"""
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="ref_s", user_id="uuu")
    ms.clear_turn_send_throttle("ref_s", "turn_ref")

    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()) as scr,
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
    ):
        ctx = _make_ctx(ev, "turn_ref", bot)
        ctx.tool_call_id = "call_refuse"
        result = _run(
            ms.send_message_by_ai(
                ctx,
                text="让 render_agent 去出这张卡",
                image_id="http://example.test/card.png",
            )
        )

    assert result.startswith("未发送。")
    assert "内部委派" in result
    assert scr.await_count == 0
    assert bot.send.await_count == 0


def test_throttle_resets_on_new_turn_and_after_clear():
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="reset_s", user_id="u1")
    ms.clear_turn_send_throttle("reset_s", "turn_1")
    ms.clear_turn_send_throttle("reset_s", "turn_2")

    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()),
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
    ):
        c1 = _make_ctx(ev, "turn_1", bot)
        _run(ms.send_message_by_ai(c1, text="a"))
        _run(ms.send_message_by_ai(c1, text="b"))
        blocked = _run(ms.send_message_by_ai(c1, text="c"))
        assert "不是常规回复通道" in blocked

        # 换新回合额度重置
        c2 = _make_ctx(ev, "turn_2", bot)
        ok_new_turn = _run(ms.send_message_by_ai(c2, text="new-turn"))
        assert "消息已发送" in ok_new_turn

        ms.clear_turn_send_throttle("reset_s", "turn_1")
        c1b = _make_ctx(ev, "turn_1", bot)
        ok_after_clear = _run(ms.send_message_by_ai(c1b, text="after-clear"))
        assert "消息已发送" in ok_after_clear


def test_send_does_not_mark_turn_terminal() -> None:
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="s_wait", user_id="u1")
    extra: dict[str, object] = {"turn_id": "t_wait", "speech_policy": "free", "has_status_tool": False}
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=bot, ev=ev, extra=extra, parent_session_id=None)
    ms.clear_turn_send_throttle("s_wait", "t_wait")
    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()),
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
    ):
        result = _run(ms.send_message_by_ai(ctx, text="查到了，出门带伞。"))
    assert "消息已发送" in result
    assert "delivered_with_speech" not in extra


def test_status_ok_refuses_without_status_tool() -> None:
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="s_st", user_id="u1")
    ev.raw_text = "图呢"
    extra: dict[str, object] = {"turn_id": "t_st", "speech_policy": "status_ok", "has_status_tool": False}
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=bot, ev=ev, extra=extra, parent_session_id=None)
    ms.clear_turn_send_throttle("s_st", "t_st")
    result = _run(ms.send_message_by_ai(ctx, text="做完了"))
    assert "追问进度" in result
    assert bot.send.await_count == 0


def test_nickname_at_becomes_user_id_or_drops() -> None:
    from gsuid_core.ai_core.utils import rewrite_nickname_mentions

    names = {"小明": "10001", "小红": "10002"}
    dropped = rewrite_nickname_mentions("图好了。<@小明>", names, already_at="10001")
    assert "@" not in dropped
    assert "图好了" in dropped
    rewritten = rewrite_nickname_mentions("看这个 <@小明>", names)
    assert "<@10001>" in rewritten
    assert "小明" not in rewritten
    spaced = rewrite_nickname_mentions("看这个 < @  小明  >", names)
    assert "<@10001>" in spaced
    escaped = rewrite_nickname_mentions("看这个 &lt;@小明&gt;", names)
    assert "<@10001>" in escaped
    fullwidth = rewrite_nickname_mentions("看这个 ＜@小明＞", names)
    assert "<@10001>" in fullwidth
    slashed = rewrite_nickname_mentions("看这个 \\<@小明\\>", names)
    assert "<@10001>" in slashed
    entity = rewrite_nickname_mentions("看这个 &#60;@小明&#62;", names)
    assert "<@10001>" in entity
    unknown = rewrite_nickname_mentions("嗨 <@不存在的人>，在吗", names)
    assert "@" not in unknown
    assert "，在吗" in unknown
    bare = "@了别人就去找他要嘛，你这不是拿我寻开心"
    assert rewrite_nickname_mentions(bare, names) == bare
    assert rewrite_nickname_mentions("看这个 @小明", names) == "看这个 @小明"


def test_delivery_wake_ignores_model_user_id() -> None:
    """回灌轮 C 端 @ 跟 ev.user_id（任务发起人），模型乱填 user_id 无效。"""
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="wake_s", user_id="owner-b")
    extra: dict[str, object] = {"turn_id": "t_wake", "delivery_wake": True}
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=bot, ev=ev, extra=extra, parent_session_id=None)
    ms.clear_turn_send_throttle("wake_s", "t_wake")
    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()) as scr,
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
    ):
        result = _run(ms.send_message_by_ai(ctx, text="给你。", user_id="master-a"))
    assert "消息已发送" in result
    assert scr.await_count == 1
    assert scr.await_args is not None
    assert scr.await_args.kwargs["at_user_id"] == "owner-b"
    assert extra["at_user_id"] == "owner-b"


def test_cross_owner_res_handle_is_refused() -> None:
    """回灌时不得把 A 的 res_ 发给 B。"""
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.outbound import ResOwner
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="own_s", user_id="user-b")
    extra: dict[str, object] = {"turn_id": "t_own", "delivery_wake": True}
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=bot, ev=ev, extra=extra, parent_session_id=None)
    ms.clear_turn_send_throttle("own_s", "t_own")
    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()) as scr,
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
        patch(
            "gsuid_core.ai_core.outbound.lookup_res_owner",
            new=AsyncMock(return_value=ResOwner(True, "user-a")),
        ),
    ):
        result = _run(ms.send_message_by_ai(ctx, image_id="res_deadbeef01"))
    assert "其它发起人" in result
    assert bot.send.await_count == 0
    assert scr.await_count == 0


def test_model_user_id_cannot_take_another_owners_image() -> None:
    """非回灌轮也只认 ev.user_id。模型把发起人填进 user_id 不能把图发出去。"""
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.outbound import ResOwner
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="bypass_s", user_id="user-b")
    extra: dict[str, object] = {"turn_id": "t_bypass"}
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=bot, ev=ev, extra=extra, parent_session_id=None)
    ms.clear_turn_send_throttle("bypass_s", "t_bypass")
    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()) as scr,
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
        patch(
            "gsuid_core.ai_core.outbound.lookup_res_owner",
            new=AsyncMock(return_value=ResOwner(True, "user-a")),
        ),
        patch(
            "gsuid_core.ai_core.outbound.try_claim_image_delivery",
            new=AsyncMock(side_effect=AssertionError("refused send must not claim")),
        ),
    ):
        result = _run(ms.send_message_by_ai(ctx, image_id="res_ownedbya01", user_id="user-a"))
    assert "其它发起人" in result
    assert bot.send.await_count == 0
    assert scr.await_count == 0


def test_res_image_without_speaker_is_refused() -> None:
    """当前对话没有说话人时，不能靠模型填的 user_id 把图发出去。"""
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.outbound import ResOwner
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="nospeaker_s", user_id="")
    extra: dict[str, object] = {"turn_id": "t_nospeaker"}
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=bot, ev=ev, extra=extra, parent_session_id=None)
    ms.clear_turn_send_throttle("nospeaker_s", "t_nospeaker")
    with (
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
        patch(
            "gsuid_core.ai_core.outbound.lookup_res_owner",
            new=AsyncMock(return_value=ResOwner(True, "user-a")),
        ),
        patch(
            "gsuid_core.ai_core.outbound.try_claim_image_delivery",
            new=AsyncMock(side_effect=AssertionError("refused send must not claim")),
        ),
    ):
        result = _run(ms.send_message_by_ai(ctx, image_id="res_ownedbya02", user_id="user-a"))
    assert "没有说话人" in result
    assert bot.send.await_count == 0


def test_res_image_without_owner_record_is_refused() -> None:
    """产物在、发起人记录是空的，不能当成无主图发出去。"""
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.outbound import ResOwner
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="noowner_s", user_id="user-b")
    extra: dict[str, object] = {"turn_id": "t_noowner"}
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=bot, ev=ev, extra=extra, parent_session_id=None)
    ms.clear_turn_send_throttle("noowner_s", "t_noowner")
    with (
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
        patch(
            "gsuid_core.ai_core.outbound.lookup_res_owner",
            new=AsyncMock(return_value=ResOwner(True, "")),
        ),
        patch(
            "gsuid_core.ai_core.outbound.try_claim_image_delivery",
            new=AsyncMock(side_effect=AssertionError("refused send must not claim")),
        ),
    ):
        result = _run(ms.send_message_by_ai(ctx, image_id="res_blankowner1"))
    assert "没有发起人" in result
    assert bot.send.await_count == 0


def test_speaker_can_send_own_res_image() -> None:
    """当前说话人就是发起人时，图要发出去。"""
    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.outbound import ResOwner, ImageClaim
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="ownsend_s", user_id="user-a")
    extra: dict[str, object] = {"turn_id": "t_ownsend"}
    ctx = MagicMock()
    ctx.deps = ToolContext(bot=bot, ev=ev, extra=extra, parent_session_id=None)
    ms.clear_turn_send_throttle("ownsend_s", "t_ownsend")
    with (
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
        patch(
            "gsuid_core.ai_core.outbound.lookup_res_owner",
            new=AsyncMock(return_value=ResOwner(True, "user-a")),
        ),
        patch(
            "gsuid_core.ai_core.outbound.try_claim_image_delivery",
            new=AsyncMock(return_value=ImageClaim(occupied=True, refuse=None)),
        ),
        patch(
            "gsuid_core.ai_core.buildin_tools.message_sender._resolve_kanban_artifact",
            new=AsyncMock(return_value=b"png-bytes"),
        ),
        patch(
            "gsuid_core.ai_core.buildin_tools.message_sender.RM.register",
            return_value="img_testowned",
        ),
        patch("gsuid_core.ai_core.outbound.record_outbound", new=AsyncMock()),
        patch("gsuid_core.ai_core.outbound.write_decision_memo", new=AsyncMock()),
    ):
        result = _run(ms.send_message_by_ai(ctx, image_id="res_ownedbya03", user_id="user-b"))
    assert "消息已发送" in result
    assert bot.send.await_count == 1


def test_at_digits_become_at_segment() -> None:
    from gsuid_core.ai_core.utils import _parse_at_segments

    segments = _parse_at_segments("好哦 <@100000001> 你来看")
    types = [s.type for s in segments]
    assert "at" in types
    at_seg = segments[types.index("at")]
    assert at_seg.data == "100000001" or "100000001" in str(at_seg.data)
    for s in segments:
        if s.type == "text":
            assert "100000001" not in str(s.data)

    bare = _parse_at_segments("好哦 @100000001 你来看")
    assert all(s.type != "at" for s in bare)
    assert any("100000001" in str(s.data) for s in bare if s.type == "text")

    escaped = _parse_at_segments("好哦 &lt; @ 100000001 &gt; 你来看")
    esc_types = [s.type for s in escaped]
    assert "at" in esc_types
    assert escaped[esc_types.index("at")].data == "100000001"

    cq = _parse_at_segments("好哦 [CQ:at,qq=100000001] 你来看")
    cq_types = [s.type for s in cq]
    assert "at" in cq_types
    assert cq[cq_types.index("at")].data == "100000001"


def test_has_model_visible_content_covers_modalities() -> None:
    from gsuid_core.models import Event
    from gsuid_core.ai_core.utils import has_model_visible_content

    def _ev(**overrides: Any) -> Event:
        fields: dict[str, Any] = {"bot_id": "onebot", "bot_self_id": "1", "msg_id": "m", "user_type": "group"}
        fields.update(overrides)
        return Event(**fields)

    assert has_model_visible_content(_ev()) is False
    assert has_model_visible_content(_ev(text="在吗")) is True
    assert has_model_visible_content(_ev(image_id_list=["img_1"])) is True
    assert has_model_visible_content(_ev(audio_id="aud_1")) is True
    assert has_model_visible_content(_ev(audio_id_list=["aud_2"])) is True
    assert has_model_visible_content(_ev(file="base64data")) is True
    assert has_model_visible_content(_ev(video_id_list=["vid_1"])) is True


def test_send_message_by_ai_multiple_image_ids_success() -> None:
    """多图批量发送：单次调用打包为一个消息发出。"""
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="multi_s", user_id="u1")
    ms.clear_turn_send_throttle("multi_s", "turn_multi")

    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()),
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
        patch("gsuid_core.ai_core.outbound.record_outbound", new=AsyncMock()),
        patch("gsuid_core.ai_core.outbound.write_decision_memo", new=AsyncMock()),
    ):
        ctx = _make_ctx(ev, "turn_multi", bot)
        result = _run(
            ms.send_message_by_ai(
                ctx,
                text="看这三张图",
                image_ids=[
                    "http://example.com/1.png",
                    "http://example.com/2.png",
                    "http://example.com/3.png",
                ],
            )
        )

    assert "消息已发送" in result
    assert bot.send.await_count == 1
    sent_msg = bot.send.await_args.args[0]
    assert isinstance(sent_msg, list)
    image_segs = [s for s in sent_msg if s.type == "image"]
    assert len(image_segs) == 3


def test_send_message_by_ai_partial_image_failure() -> None:
    """多图部分失败：有效的正常发出，失败的记在回执中。"""
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="part_s", user_id="u1")
    ms.clear_turn_send_throttle("part_s", "turn_part")

    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()),
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
        patch("gsuid_core.ai_core.outbound.record_outbound", new=AsyncMock()),
        patch("gsuid_core.ai_core.outbound.write_decision_memo", new=AsyncMock()),
        patch(
            "gsuid_core.utils.resource_manager.RM.get",
            new=AsyncMock(side_effect=ValueError("找不到资源")),
        ),
    ):
        ctx = _make_ctx(ev, "turn_part", bot)
        result = _run(
            ms.send_message_by_ai(
                ctx,
                image_ids=[
                    "http://example.com/ok.png",
                    "img_not_exist",
                ],
            )
        )

    assert "消息已发送" in result
    assert "部分图片未发送" in result
    assert "img_not_exist" in result
    assert bot.send.await_count == 1
    sent_msg = bot.send.await_args.args[0]
    # 只有 1 张成功，可能是单个 Segment 或列表
    segs = sent_msg if isinstance(sent_msg, list) else [sent_msg]
    image_segs = [s for s in segs if s.type == "image"]
    assert len(image_segs) == 1


def test_send_message_by_ai_string_and_list_robustness() -> None:
    """测试模型传参容错：image_ids 传单字符串、image_id 传列表、JSON数组字符串等形态。"""
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    ev = _make_ev(session_id="s_robust", user_id="user-a")
    bot = MagicMock()
    bot.send = AsyncMock(return_value={"status": "ok"})

    ctx = _make_ctx(ev, "turn_rob1", bot)
    # 1. image_ids 传单个字符串
    res1 = _run(ms.send_message_by_ai(ctx, image_ids="http://example.com/str.png"))
    assert "消息已发送" in res1
    assert bot.send.await_count == 1

    # 2. image_id 传列表
    ctx2 = _make_ctx(ev, "turn_rob2", bot)
    res2 = _run(
        ms.send_message_by_ai(
            ctx2,
            image_id=["http://example.com/a.png", "http://example.com/b.png"],
        )
    )
    assert "消息已发送" in res2

    # 3. image_ids 传 JSON 数组字符串
    ctx3 = _make_ctx(ev, "turn_rob3", bot)
    res3 = _run(
        ms.send_message_by_ai(
            ctx3,
            image_ids='["http://example.com/j1.png", "http://example.com/j2.png"]',
        )
    )
    assert "消息已发送" in res3

    # 4. 同时传 image_ids 和 image_id
    ctx4 = _make_ctx(ev, "turn_rob4", bot)
    res4 = _run(
        ms.send_message_by_ai(
            ctx4,
            image_ids=["http://example.com/both1.png"],
            image_id="http://example.com/both2.png",
        )
    )
    assert "消息已发送" in res4


if __name__ == "__main__":
    test_text_routes_through_send_chat_result_and_not_raw_send()
    test_per_turn_throttle_rejects_third_call()
    test_throttle_resets_on_new_turn_and_after_clear()
    test_send_message_by_ai_multiple_image_ids_success()
    test_send_message_by_ai_partial_image_failure()
    test_send_message_by_ai_string_and_list_robustness()
    print("ALL PASS")
