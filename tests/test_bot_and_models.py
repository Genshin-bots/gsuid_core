"""消息模型、人格形态档、气泡拆条与多模态图片规范化测试。"""

from __future__ import annotations

import io
import zlib
import base64
import struct
import asyncio
from typing import List, Union, Mapping, Optional
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

import pytest
from PIL import Image

from gsuid_core.bot import Bot, _Bot
from tests._support import isolated_history
from gsuid_core.models import Event
from gsuid_core.ai_core import delivery as ai_delivery
from gsuid_core.segment import Message
from gsuid_core.ai_core.persona.chat_style import (
    DEFAULT_CHAT_STYLE,
    _style_from,
    speech_shape_spec,
    resolve_chat_style,
)

_SendPayload = Union[Message, List[Message], str, bytes, List[str]]


def _plain_text(message: _SendPayload) -> str:
    if isinstance(message, str):
        return message
    segs = message if isinstance(message, list) else [message]
    out = ""
    for seg in segs:
        if isinstance(seg, Message):
            data = seg.data
            out += data if isinstance(data, str) else ""
    return out


async def _no_sleep(_seconds: float) -> None:
    return None


def _drive(
    text: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bubbles: Optional[int] = None,
    on_sleep: Callable[[float], Awaitable[None]] = _no_sleep,
    ev: Optional[Event] = None,
    bot: Optional[Bot] = None,
) -> List[_SendPayload]:
    sent: List[_SendPayload] = []

    async def _fake_send(
        self: Bot,
        message: _SendPayload,
        at_sender: bool = False,
        extra_metadata: Optional[Mapping[str, object]] = None,
        wait_recall: bool = False,
    ) -> None:
        sent.append(message)

    monkeypatch.setattr(ai_delivery.asyncio, "sleep", on_sleep)
    monkeypatch.setattr(Bot, "send", _fake_send)
    if bubbles is not None:
        monkeypatch.setattr(ai_delivery, "_persona_max_bubbles", lambda _ev: bubbles)
    target_bot = bot if bot is not None else Bot.__new__(Bot)
    asyncio.run(ai_delivery.send_chat_result(target_bot, text, ev=ev, ooc_check=False))
    return sent


def _run(text: str, monkeypatch: pytest.MonkeyPatch, *, bubbles: Optional[int] = None) -> List[str]:
    return [_plain_text(message) for message in _drive(text, monkeypatch, bubbles=bubbles)]


def test_bot_wrapper_and_connection_states() -> None:
    """Bot/_Bot 包装契约（§8）与连接留存窗、回执能力三态机。"""
    from gsuid_core.bot import BOT_RETAIN_TIMEOUT, BotConnectionState, RecallCapabilityState

    low = _Bot("ws-alpha")
    ev = Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="group_1",
        user_id="100000001",
        WS_BOT_ID="ws-alpha",
    )
    high = Bot(low, ev)
    assert high.bot is low and high.ev is ev and not hasattr(low, "ev")
    assert high.bot_id == ev.bot_id

    assert low.connection_state == BotConnectionState.EXPIRED
    low._disconnected_at = 1000.0
    assert low.get_connection_state(now=1000.0 + BOT_RETAIN_TIMEOUT - 1) == BotConnectionState.RETAINED
    assert low.get_connection_state(now=1000.0 + BOT_RETAIN_TIMEOUT + 1) == BotConnectionState.EXPIRED

    assert low.recall_capability_state == RecallCapabilityState.PROBING
    low._supports_recall = True
    assert low.recall_capability_state == RecallCapabilityState.SUPPORTED
    low._supports_recall = False
    assert low.recall_capability_state == RecallCapabilityState.UNSUPPORTED


def test_chat_style_contract(tmp_path: Path) -> None:
    from gsuid_core.utils.plugins_config.models import GsIntConfig
    from gsuid_core.utils.plugins_config.gs_config import StringConfig

    spec = speech_shape_spec()
    assert resolve_chat_style(None).bubbles == 2

    def _mk_cfg(name: str, style: int, soft: int | None = None) -> StringConfig:
        soft_val = spec["soft_default"] if soft is None else soft
        return StringConfig(
            name,
            tmp_path / f"{name}.json",
            {
                "chat_style": GsIntConfig("风格", "测试风格", style),
                "speech_len_soft": GsIntConfig("建议长度", "测试长度", soft_val),
            },
        )

    low_style = _style_from(_mk_cfg("low", spec["chat_style_min"]))
    mid_style = _style_from(_mk_cfg("mid", DEFAULT_CHAT_STYLE))
    high_style = _style_from(_mk_cfg("high", spec["chat_style_max"]))
    assert low_style.bubbles == 1 and mid_style.bubbles == 2 and high_style.bubbles >= 3

    explicit_soft = spec["soft_default"] + 17
    override_style = _style_from(_mk_cfg("override", spec["chat_style_min"], soft=explicit_soft))
    assert override_style.bubbles == 1 and override_style.soft == explicit_soft

    banned = ("唔", "呼", "zzz", "早柚", "貉", "股票", "黄金", "原神", "提瓦特")
    for tier in spec["tiers"]:
        hint = tier["segment_hint"]
        assert hint and not any(word in hint for word in banned)


def test_send_chat_result_splitting_and_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.ai_core.delivery import pack_outbound_bubbles
    from gsuid_core.ai_core.agent_errors import strip_framework_user_leaks

    # 框架泄漏剥除：控制信封被剥、段间空行必须保留（拆气泡依赖 \n\s*\n）
    assert strip_framework_user_leaks("第一句。\n\n第二句。") == "第一句。\n\n第二句。"
    leaked = strip_framework_user_leaks("<control>内部指令</control>正文。")
    assert "<control>" not in leaked and "正文。" in leaked
    assert pack_outbound_bubbles(["单段"], 0) == []

    out = _run("第一句\n\n第二句", monkeypatch)
    assert [s.strip() for s in out] == ["第一句", "第二句"]

    for limit in (1, 2, 4):
        parts = _run("一\n\n二\n\n三\n\n四", monkeypatch, bubbles=limit)
        assert len(parts) == limit
        if limit == 2:
            assert parts[0].strip() == "一" and "二" in parts[1] and "四" in parts[1]

    sent = _drive("[CQ:at,qq=100000002,name=foo] 到时间啦", monkeypatch)
    assert len(sent) == 1 and isinstance(sent[0], list)
    pairs = [(seg.type, seg.data) for seg in sent[0] if isinstance(seg, Message)]
    assert pairs == [("at", "100000002"), ("text", " 到时间啦")]

    # 首气泡打字停顿（sleep）期间若有新用户消息插队，唤醒后自动补挂 Message("reply", msg_id)
    from gsuid_core.ai_core.inbox.turn import InboxTurn, bind_turn, reset_turn
    from gsuid_core.ai_core.inbox.state import WakeItem, PendingEntry

    ev_u1 = Event("OneBot", "900000001", "mid_1", "group", "group_pause_1", "100000001", {"nickname": "测试用户A"}, 6)
    ev_u2 = Event("OneBot", "900000001", "mid_2", "group", "group_pause_1", "100000002", {"nickname": "测试用户B"}, 6)
    with isolated_history(ev_u1) as hm:
        rec1 = hm.add_message(ev_u1, "user", "测试提问", user_name="测试用户A", metadata={"msg_id": "mid_1"})
        assert rec1.link is not None
        turn = InboxTurn(
            session_id=ev_u1.session_id,
            persona_name="测试人格",
            entries=(
                PendingEntry(
                    WakeItem(rec1.link.seq, "100000001", "name", "hard", 1.0),
                    ev_u1,
                    persona_name="测试人格",
                ),
            ),
            from_seq=0,
            head_seq=rec1.link.seq,
        )
        interjected = [False]

        async def _interject_on_sleep(_sec: float) -> None:
            if not interjected[0]:
                interjected[0] = True
                hm.add_message(ev_u2, "user", "测试插话", user_name="测试用户B", metadata={"msg_id": "mid_2"})

        tok = bind_turn(turn)
        try:
            captured = _drive(
                f"<reply #{rec1.link.seq}>\n第一段回应。\n\n第二段回应。",
                monkeypatch,
                bubbles=2,
                on_sleep=_interject_on_sleep,
                ev=ev_u1,
                bot=Bot(_Bot("ws_p1"), ev_u1),
            )
        finally:
            reset_turn(tok)
        assert len(captured) == 2 and isinstance(captured[0], list) and isinstance(captured[1], list)
        seg0 = [(s.type, s.data) for s in captured[0] if isinstance(s, Message)]
        seg1 = [(s.type, s.data) for s in captured[1] if isinstance(s, Message)]
        assert seg0[0] == ("reply", "mid_1")
        assert all(t != "reply" for t, _ in seg1)


def test_event_link_seq_reply_and_at_self() -> None:
    """EventLink：seq 单调递增、引用边解析到 uid/self/None、自动@不置 at_self、dict 往返保 link。"""
    from gsuid_core.message_history import MessageRecord, HistoryManager

    mgr = HistoryManager()

    def ev(uid: str, nick: str) -> Event:
        return Event("OneBot", "900000001", "", "group", "70001", uid, {"nickname": nick}, 6)

    c_ev = ev("100000003", "测试用户C")
    b_ev = ev("100000002", "测试用户B")
    bot_ev = ev("900000001", "测试人格")

    u1 = mgr.add_message(c_ev, "user", "让你画图", user_name="测试用户C", metadata={"msg_id": "m1"})
    bot1 = mgr.add_message(
        bot_ev,
        "assistant",
        "唔…好",
        user_name="测试人格",
        metadata={"msg_id": "b1", "speech_channel": "persona"},
    )
    assert u1.link is not None and bot1.link is not None and u1.link.seq < bot1.link.seq

    cases = [
        # (reply_id, at_bot, 期望 reply_to_author, 期望 reply_to_seq, 期望 at_self)
        ("m1", False, "100000003", u1.link.seq, False),  # 用户引用用户
        ("b1", True, "self", bot1.link.seq, False),  # 引用 Bot 自带自动@不算 at_self
        ("b1", False, "self", bot1.link.seq, False),  # 用户引用 Bot
        ("lost", True, None, None, True),  # 引用对象已出窗口：无引用边，真@仍算
        (None, True, None, None, True),  # 纯 @
        (None, False, None, None, False),
    ]
    last_seq = bot1.link.seq
    for i, (reply_id, at_bot, author, rseq, aself) in enumerate(cases):
        meta: dict[str, object] = {"msg_id": f"q{i}", "at_bot": at_bot}
        if reply_id is not None:
            meta["reply_id"] = reply_id
        rec = mgr.add_message(b_ev, "user", f"case{i}", user_name="测试用户B", metadata=meta)
        link = rec.link
        assert link is not None
        assert link.reply_to_author == author, f"case{i}"
        assert link.reply_to_seq == rseq, f"case{i}"
        assert link.at_self is aself, f"case{i}"
        assert link.seq > last_seq
        last_seq = link.seq
        rt = MessageRecord.from_dict(rec.to_dict())
        assert rt.link == link

    assert mgr.head_seq(b_ev) == last_seq
    after = mgr.records_after(b_ev, bot1.link.seq)
    assert [r.link.seq for r in after if r.link] == list(range(bot1.link.seq + 1, last_seq + 1))
    found = mgr.find_by_seq(b_ev, u1.link.seq)
    assert found is not None and found.content == "让你画图"

    # 引用对象出窗时，QQ 随引用自动附带的 quote_auto_at 必须落为 quote_self 而非伪造 at_self
    auto_rec = mgr.add_message(
        b_ev,
        "user",
        "这",
        user_name="测试用户B",
        metadata={"msg_id": "q_auto", "at_bot": True, "quote_auto_at": True, "reply_id": "lost_proactive"},
    )
    assert auto_rec.link is not None
    assert auto_rec.link.reply_to_author == "self"
    assert auto_rec.link.at_self is False

    # 出站缺失 bot_self_id 时从活跃会话反查真实身份；echo 延迟回写 msg_id 后清掉负缓存
    res_bid, res_self = mgr.resolve_session_identity(
        ws_bot_id=None, bot_id="Bot", target_type="group", target_id="70001"
    )
    assert (res_bid, res_self) == ("OneBot", "900000001")
    delayed = mgr.add_message(bot_ev, "assistant", "主动播报", user_name="AI", metadata={})
    assert mgr.find_by_msg_id(b_ev, "late_mid_1") is None
    mgr.attach_msg_id(delayed, "late_mid_1")
    assert mgr.find_by_msg_id(b_ev, "late_mid_1") is delayed
    assert delayed.link is not None and delayed.link.msg_id == "late_mid_1"


def test_detached_turn_scope_isolates_turn_and_outbound() -> None:
    """detached_turn_scope 同时隔离前台轮次与出站回复绑定上下文，退出后完整恢复。"""
    from gsuid_core.ai_core.inbox.turn import InboxTurn, bind_turn, reset_turn, current_turn, detached_turn_scope
    from gsuid_core.message_history.link import OutboundBinding, bind_outbound, reset_outbound, current_outbound

    turn = InboxTurn(session_id="group:200000001", persona_name="测试角色", entries=(), from_seq=0, head_seq=1)
    binding = OutboundBinding(seen_seq=1, in_reply_to_seqs=(1,))
    turn_tok = bind_turn(turn)
    out_tok = bind_outbound(binding)
    try:
        assert current_turn() is turn and current_outbound() is binding
        with detached_turn_scope():
            assert current_turn() is None
            assert current_outbound() is None
        assert current_turn() is turn and current_outbound() is binding
    finally:
        reset_outbound(out_tok)
        reset_turn(turn_tok)


def test_convert_message_link_image_to_base64_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """link:// 图片按 base64 模式转换时，首次下载与缓存命中均须带 base64:// 前缀。"""
    from gsuid_core import segment

    @dataclass(frozen=True)
    class _Stub:
        content: bytes = b""
        data: str = "base64"

    raw_bytes = b"\x89PNG\r\n\x1a\ntest-image"
    expected = f"base64://{base64.b64encode(raw_bytes).decode('utf-8')}"
    calls: list[str] = []

    async def _fake_sget(url: str) -> _Stub:
        calls.append(url)
        return _Stub(content=raw_bytes)

    async def _fake_global_val(_bot_id: str, _bot_self_id: str) -> dict[str, int]:
        return {"image": 0}

    monkeypatch.setattr(segment, "URL_MAP", {})
    monkeypatch.setattr(segment, "sget", _fake_sget)
    monkeypatch.setattr(segment, "get_global_val", _fake_global_val)
    monkeypatch.setattr(segment.send_pic_config, "get_config", lambda *_a: _Stub())

    msg = Message(type="image", data="link://https://example.invalid/sample.png")
    first = asyncio.run(segment.convert_message(msg, "onebot", "self1"))
    second = asyncio.run(segment.convert_message(msg, "onebot", "self1"))

    assert first == [Message(type="image", data=expected)]
    assert second == [Message(type="image", data=expected)]
    assert calls == ["https://example.invalid/sample.png"]


def test_prepare_video_without_image_support_never_extracts_frames() -> None:
    """模型未声明 image 能力时，视频投喂与超限回退均不得抽帧，须返回文本占位。"""
    from pydantic_ai.messages import BinaryContent

    from gsuid_core.ai_core.multimodal.openai_files import video_delivery_mode
    from gsuid_core.ai_core.multimodal.inbound_prepare import (
        InboundMediaContext,
        prepare_video_content,
    )

    for prov in ("openai", "gemini", "anthropic"):
        assert video_delivery_mode(supports_video=False, supports_image=False, provider=prov) == "unavailable"

    sample = BinaryContent(data=b"0" * 16, media_type="video/mp4")
    no_media_ctx = InboundMediaContext(
        model=None,
        provider="openai",
        model_support=["text"],
        task_level="high",
        task_kind=None,
        openai_video_settings=("file", 12, "off"),
    )
    oversize_video_only_ctx = InboundMediaContext(
        model=None,
        provider="openai",
        model_support=["text", "video"],
        task_level="high",
        task_kind=None,
        openai_video_settings=("video_url", 0, "off"),
    )
    for ctx in (no_media_ctx, oversize_video_only_ctx):
        out = asyncio.run(prepare_video_content([sample], ctx))
        assert len(out) == 1 and isinstance(out[0], str)
        assert "无法读取该视频内容" in out[0]


def test_image_normalization_animation_and_model_adaptation() -> None:
    """图片转码、动图合成与分帧拼图、跨模型图片互转不原地改写及失效图剥除。"""
    from pydantic_ai.messages import ImageUrl, ModelRequest, BinaryContent, ToolReturnPart, UserPromptPart

    from gsuid_core.utils.image.image_io import (
        sniff_image_mime,
        normalize_inline_image,
        to_provider_image_content,
        adapt_messages_images_for_model,
    )
    from gsuid_core.utils.resource_manager import ResourceManager
    from gsuid_core.ai_core.history_compact import (
        _is_image_payload_error,
        _strip_remote_images_from_history,
    )
    from gsuid_core.utils.image.image_tools import change_ev_image_to_bytes
    from gsuid_core.ai_core.multimodal.animated_frames import _composite_frames, render_animation_sheets

    bmp_buf = io.BytesIO()
    Image.new("RGB", (32, 32), (120, 200, 80)).save(bmp_buf, format="BMP")
    bmp_b64 = "base64://" + base64.b64encode(bmp_buf.getvalue()).decode("ascii")
    gemini_injected, gemini_err = to_provider_image_content(bmp_b64, provider_or_model="google")
    assert gemini_err is None and gemini_injected is not None and len(gemini_injected) == 1
    assert isinstance(gemini_injected[0], BinaryContent) and gemini_injected[0].media_type == "image/jpeg"

    pil_img = Image.new("RGBA", (16, 16), (255, 0, 0, 128))
    local_rm = ResourceManager()
    rid = local_rm.register(pil_img)
    rm_bytes = asyncio.run(local_rm.get(rid))
    assert sniff_image_mime(rm_bytes) == "image/png"

    webp_buf = io.BytesIO()
    Image.new("RGB", (12, 12), (10, 20, 30)).save(webp_buf, format="WEBP")
    webp_raw_b64 = base64.b64encode(webp_buf.getvalue()).decode("ascii")
    webp_data_uri = f"data:image/webp;base64,{webp_raw_b64}"
    assert sniff_image_mime(asyncio.run(change_ev_image_to_bytes(webp_data_uri))) == "image/webp"
    assert sniff_image_mime(asyncio.run(change_ev_image_to_bytes(webp_raw_b64))) == "image/webp"

    def _png_chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    def _rgba_idat(w: int, h: int, rgba: tuple[int, int, int, int]) -> bytes:
        row = b"\x00" + bytes(rgba) * w
        return zlib.compress(row * h)

    apng_bytes = b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 6, 0, 0, 0)),
            _png_chunk(b"acTL", struct.pack(">II", 3, 0)),
            _png_chunk(b"fcTL", struct.pack(">IIIIIHHBB", 0, 16, 16, 0, 0, 1, 10, 0, 0)),
            _png_chunk(b"IDAT", _rgba_idat(16, 16, (255, 0, 0, 255))),
            _png_chunk(b"fcTL", struct.pack(">IIIIIHHBB", 1, 8, 8, 4, 4, 1, 10, 2, 1)),
            _png_chunk(b"fdAT", struct.pack(">I", 2) + _rgba_idat(8, 8, (0, 255, 0, 255))),
            _png_chunk(b"fcTL", struct.pack(">IIIIIHHBB", 3, 4, 4, 0, 0, 1, 10, 0, 0)),
            _png_chunk(b"fdAT", struct.pack(">I", 4) + _rgba_idat(4, 4, (0, 0, 255, 255))),
            _png_chunk(b"IEND", b""),
        ]
    )
    norm_apng = normalize_inline_image(apng_bytes, flatten_animation=False)
    assert norm_apng == (apng_bytes, "image/png")
    comp_apng = _composite_frames(Image.open(io.BytesIO(apng_bytes)))
    assert len(comp_apng) == 3
    assert comp_apng[1].getpixel((12, 12)) == (255, 0, 0, 255)
    assert comp_apng[1].getpixel((6, 6)) == (0, 255, 0, 255)
    assert comp_apng[2].getpixel((6, 6)) == (255, 0, 0, 255)
    assert comp_apng[2].getpixel((1, 1)) == (0, 0, 255, 255)
    apng_sheets = render_animation_sheets(apng_bytes)
    assert apng_sheets is not None and len(apng_sheets.images) == 3

    gif_buf = io.BytesIO()
    gif_frames = [Image.new("RGB", (24, 24), (idx * 10, 50, 100)) for idx in range(20)]
    gif_frames[0].save(gif_buf, format="GIF", save_all=True, append_images=gif_frames[1:], duration=50)
    gif_sheets = render_animation_sheets(gif_buf.getvalue())
    assert gif_sheets is not None and len(gif_sheets.images) == 5
    sheet0 = Image.open(io.BytesIO(gif_sheets.images[0]))
    assert sheet0.width == sheet0.height and sheet0.width >= 600

    png_buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(png_buf, format="PNG")
    png_data_uri = f"data:image/png;base64,{base64.b64encode(png_buf.getvalue()).decode('ascii')}"
    orig_user_part = UserPromptPart(content=["测试图片消息", ImageUrl(url=png_data_uri)])
    orig_tool_part = ToolReturnPart(
        tool_name="read_image",
        content=[ImageUrl(url=png_data_uri)],
        tool_call_id="call_img_1",
    )
    orig_req = ModelRequest(parts=[orig_user_part, orig_tool_part])
    google_adapted = adapt_messages_images_for_model([orig_req], "google")
    assert isinstance(orig_user_part.content[1], ImageUrl)
    adapted_req = google_adapted[0]
    assert isinstance(adapted_req, ModelRequest)
    adapted_up = adapted_req.parts[0]
    adapted_tr = adapted_req.parts[1]
    assert isinstance(adapted_up, UserPromptPart) and isinstance(adapted_up.content[1], BinaryContent)
    assert isinstance(adapted_tr, ToolReturnPart) and isinstance(adapted_tr.content, list)
    assert isinstance(adapted_tr.content[0], BinaryContent)

    openai_adapted = adapt_messages_images_for_model(google_adapted, "openai")
    openai_req = openai_adapted[0]
    assert isinstance(openai_req, ModelRequest)
    openai_up = openai_req.parts[0]
    assert isinstance(openai_up, UserPromptPart) and isinstance(openai_up.content[1], ImageUrl)
    assert openai_up.content[1].url.startswith("data:image/png;base64,")

    svg_b64 = "base64://" + base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>").decode("ascii")
    svg_injected, svg_err = to_provider_image_content(svg_b64, provider_or_model="openai")
    assert svg_injected is None and svg_err is not None and "不是有效图片" in svg_err

    assert _is_image_payload_error("Invalid request: invalid base64 image_url trace_id: 123")
    assert _is_image_payload_error('URL protocol "data" is not allowed. Only http:// and https:// are supported.')
    assert _is_image_payload_error(".messages[8].image[0]: You have uploaded an unsupported image.")
    history_req = ModelRequest(
        parts=[
            UserPromptPart(
                content=[
                    "用户提问",
                    ImageUrl(url="data:image/png;base64,AAAA"),
                ]
            )
        ]
    )
    assert _strip_remote_images_from_history([history_req], include_inline=False) == 0
    assert _strip_remote_images_from_history([history_req], include_inline=True) == 1
    assert isinstance(history_req.parts[0], UserPromptPart)
    assert list(history_req.parts[0].content) == ["用户提问", "[图片已失效，无法再显示]"]
