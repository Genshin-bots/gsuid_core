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


def test_cs_caption_dropped_but_image_still_sent() -> None:
    """客服收尾配图：台词丢掉，图片仍发。"""
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
    if scr.await_count:
        assert scr.await_args is not None
        sent_text = scr.await_args.args[1]
        assert "请告诉" not in sent_text
        assert "如果还需要" not in sent_text
    print("[OK] 引导追问被剥，图片仍发出")


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


def test_wait_text_does_not_set_delivered() -> None:
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
        result = _run(ms.send_message_by_ai(ctx, text="马上好。"))
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


def test_at_digits_become_at_segment() -> None:
    from gsuid_core.ai_core.utils import _parse_at_segments

    segments = _parse_at_segments("好哦 @100000001 你来看")
    types = [s.type for s in segments]
    assert "at" in types
    at_seg = segments[types.index("at")]
    assert at_seg.data == "100000001" or "100000001" in str(at_seg.data)
    for s in segments:
        if s.type == "text":
            assert "100000001" not in str(s.data)


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


if __name__ == "__main__":
    test_text_routes_through_send_chat_result_and_not_raw_send()
    test_per_turn_throttle_rejects_third_call()
    test_throttle_resets_on_new_turn_and_after_clear()
    print("ALL PASS")
