"""AI 发文件链路回归：

1. ``RM.register_file`` 登记 ``"名|payload"``，超限/空载荷拒登。
2. ``MockBot.send`` 逐段处理混合列表：图片、文字、文件各归各位（修过"只取第一段
   图片、丢同列表文字"的 bug）。
3. ``send_message_by_ai(file_ids=[...])`` 把 fil_/res_ 句柄以独立 file 段发出。
"""

import base64
import asyncio
from typing import Any
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gsuid_core.models import Message
from gsuid_core.segment import MessageSegment


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


def _fresh_call_ctx() -> dict[str, list]:
    return {"texts": [], "image_ids": [], "audio_ids": [], "video_ids": [], "file_ids": [], "bot_messages": []}


# ── RM.register_file ──────────────────────────────────────────────────────────


def test_register_file_roundtrip_and_link_skips_size_check() -> None:
    from gsuid_core.utils.resource_manager import RM, MAX_FILE_BYTES

    raw = "笔记.md|" + base64.b64encode(b"# hello").decode()
    fid = RM.register_file(raw)
    assert fid is not None and fid.startswith("fil_")
    assert RM.peek(fid) == raw

    # link:// / http(s) 载荷不校验大小：超长 URL 也照常登记
    link = "包.zip|link://https://example.com/" + "a" * (MAX_FILE_BYTES + 100)
    lid = RM.register_file(link)
    assert lid is not None
    assert RM.peek(lid) == link


def test_register_file_rejects_oversize_and_empty_payload() -> None:
    from gsuid_core.utils.resource_manager import RM, MAX_FILE_BYTES

    big = "big.bin|" + base64.b64encode(b"x" * (MAX_FILE_BYTES + 1)).decode()
    assert RM.register_file(big) is None
    assert RM.register_file("空.bin|") is None
    assert RM.register_file("没有分隔符") is None


# ── MockBot 逐段收集 ──────────────────────────────────────────────────────────


def test_mockbot_mixed_list_registers_each_segment() -> None:
    from gsuid_core.ai_core.trigger_bridge import MockBot
    from gsuid_core.utils.resource_manager import RM

    call_ctx = _fresh_call_ctx()
    mock = MockBot(MagicMock(), call_ctx)
    file_data = "报告.pdf|" + base64.b64encode(b"%PDF-fake").decode()
    asyncio.run(
        mock.send(
            [
                Message(type="image", data="base64://aGk="),
                MessageSegment.text("画好了"),
                Message(type="file", data=file_data),
            ]
        )
    )

    assert len(call_ctx["image_ids"]) == 1
    assert call_ctx["image_ids"][0].startswith("img_")
    assert len(call_ctx["file_ids"]) == 1
    assert call_ctx["file_ids"][0].startswith("fil_")
    # 同一条消息里的文字不再被图片吞掉
    assert call_ctx["bot_messages"] == ["画好了"]
    assert RM.peek(call_ctx["file_ids"][0]) == file_data


def test_mockbot_oversized_file_is_not_registered() -> None:
    from gsuid_core.ai_core.trigger_bridge import MockBot
    from gsuid_core.utils.resource_manager import MAX_FILE_BYTES

    call_ctx = _fresh_call_ctx()
    mock = MockBot(MagicMock(), call_ctx)
    big = "big.bin|" + base64.b64encode(b"x" * (MAX_FILE_BYTES + 1)).decode()
    asyncio.run(mock.send(Message(type="file", data=big)))

    assert call_ctx["file_ids"] == []
    assert len(call_ctx["bot_messages"]) == 1
    assert "big.bin" in call_ctx["bot_messages"][0]
    assert "未登记" in call_ctx["bot_messages"][0]


def test_trigger_output_announces_file_ids() -> None:
    from gsuid_core.ai_core import trigger_bridge

    call_ctx = _fresh_call_ctx()
    call_ctx["file_ids"].append("fil_ab12cd34")
    out = trigger_bridge._assemble_trigger_output(call_ctx)
    assert "已生成 1 个文件" in out
    assert "fil_ab12cd34" in out
    assert "file_ids" in out

    mcp_out = trigger_bridge._assemble_trigger_output(call_ctx, {"source": "mcp_server"})
    assert "MCP 结果未附带文件字节" in mcp_out


# ── send_message_by_ai(file_ids=...) ──────────────────────────────────────────


def test_send_message_by_ai_sends_fil_file_once() -> None:
    from gsuid_core.ai_core.buildin_tools import message_sender as ms
    from gsuid_core.utils.resource_manager import RM

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="fil_s", user_id="u1")
    ms.clear_turn_send_throttle("fil_s", "turn_fil")

    file_data = "笔记.md|" + base64.b64encode(b"# hello").decode()
    fil_id = RM.register_file(file_data)
    assert fil_id is not None

    with (
        patch("gsuid_core.ai_core.utils.send_chat_result", new=AsyncMock()),
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
        patch("gsuid_core.ai_core.outbound.record_outbound", new=AsyncMock()),
        patch("gsuid_core.ai_core.outbound.write_decision_memo", new=AsyncMock()),
    ):
        ctx = _make_ctx(ev, "turn_fil", bot)
        result = asyncio.run(ms.send_message_by_ai(ctx, file_ids=[fil_id]))

    assert "消息已发送" in result
    assert "笔记.md" in result
    assert bot.send.await_count == 1
    sent_msg = bot.send.await_args.args[0]
    assert isinstance(sent_msg, Message)
    assert sent_msg.type == "file"
    assert sent_msg.data == file_data


def test_send_message_by_ai_file_ids_rejects_bad_prefix() -> None:
    from gsuid_core.ai_core.buildin_tools import message_sender as ms

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="badfid_s", user_id="u1")
    ms.clear_turn_send_throttle("badfid_s", "turn_badfid")

    with patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False):
        ctx = _make_ctx(ev, "turn_badfid", bot)
        result = asyncio.run(ms.send_message_by_ai(ctx, file_ids=["img_abc12345"]))

    assert "只接受 fil_ / res_ 句柄" in result
    assert bot.send.await_count == 0


def test_send_message_by_ai_sends_res_file(tmp_path: Path) -> None:
    """res_ 落盘文件经归属/占位校验后按原文件名发出。"""
    from gsuid_core.ai_core.outbound import ResOwner, ImageClaim
    from gsuid_core.ai_core.buildin_tools import message_sender as ms
    from gsuid_core.ai_core.planning.models import AIAgentArtifact

    payload = tmp_path / "周报.pdf"
    payload.write_bytes(b"%PDF-1.4 fake")
    art = AIAgentArtifact(
        id="res_filetest01",
        root_task_id="t1",
        task_id="t1",
        mime="application/pdf",
        payload_path=str(payload),
    )

    bot = MagicMock()
    bot.send = AsyncMock()
    ev = _make_ev(session_id="resfile_s", user_id="user-a")
    ms.clear_turn_send_throttle("resfile_s", "turn_resfile")

    with (
        patch("gsuid_core.ai_core.output_firewall.is_enabled", return_value=False),
        patch(
            "gsuid_core.ai_core.planning.models.AIAgentArtifact.get_by_id",
            new=AsyncMock(return_value=art),
        ),
        patch(
            "gsuid_core.ai_core.outbound.lookup_res_owner",
            new=AsyncMock(return_value=ResOwner(True, "user-a")),
        ),
        patch(
            "gsuid_core.ai_core.outbound.try_claim_image_delivery",
            new=AsyncMock(return_value=ImageClaim(occupied=True, refuse=None)),
        ),
        patch("gsuid_core.ai_core.outbound.record_outbound", new=AsyncMock()),
        patch("gsuid_core.ai_core.outbound.write_decision_memo", new=AsyncMock()),
    ):
        ctx = _make_ctx(ev, "turn_resfile", bot)
        result = asyncio.run(ms.send_message_by_ai(ctx, file_ids="res_filetest01"))

    assert "消息已发送" in result
    assert "周报.pdf" in result
    assert bot.send.await_count == 1
    sent_msg = bot.send.await_args.args[0]
    assert isinstance(sent_msg, Message)
    assert sent_msg.type == "file"
    assert isinstance(sent_msg.data, str)
    name, b64 = sent_msg.data.split("|", 1)
    assert name == "周报.pdf"
    assert base64.b64decode(b64) == b"%PDF-1.4 fake"
