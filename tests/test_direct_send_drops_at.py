"""私聊出站不得带 at 段。QQ 私聊收到 type=at 会整包解析失败。"""

from __future__ import annotations

import asyncio
from typing import Literal

import pytest

from gsuid_core.bot import _Bot
from gsuid_core.models import Message, MessageSend
from gsuid_core.segment import MessageSegment

_Target = Literal["group", "direct", "channel", "sub_channel"]


def _quiet_send_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gsuid_core.bot.enable_markdown_platform", [])
    monkeypatch.setattr("gsuid_core.bot.enable_forward", "允许")


async def _sent(
    message: Message | list[Message],
    target_type: _Target,
    *,
    at_sender: bool = False,
    sender_id: str = "",
) -> list[Message] | None:
    bot = _Bot("ws-direct-at")
    task_id = "direct-at"
    await bot.target_send(
        message,
        target_type,
        "10001",
        "direct-at-platform",
        "bot-self",
        at_sender=at_sender,
        sender_id=sender_id,
        task_id=task_id,
        task_event=asyncio.Event(),
    )
    if task_id not in bot.send_dict:
        return None
    sent = bot.send_dict[task_id]
    assert isinstance(sent, MessageSend)
    assert sent.target_type == target_type
    content = sent.content
    assert content is not None
    return list(content)


def test_direct_send_drops_at_keeps_text_and_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 @ 和正文里的 at 都不进私聊帧，文字和图片仍发出。"""
    _quiet_send_path(monkeypatch)

    async def _run() -> None:
        content = await _sent(
            [
                MessageSegment.at("180910673"),
                MessageSegment.text("画好了"),
                Message(type="image", data="file://pic.png"),
            ],
            "direct",
            at_sender=True,
            sender_id="180910673",
        )
        assert content is not None
        assert [(item.type, item.data) for item in content] == [
            ("text", "画好了"),
            ("image", "file://pic.png"),
        ]

    asyncio.run(_run())


def test_direct_send_skips_frame_that_is_only_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """整帧只剩 at 时不发出。这就是 NapCat retcode 1200 的那一包。"""
    _quiet_send_path(monkeypatch)

    async def _run() -> None:
        text = await _sent(MessageSegment.text("画好了"), "direct")
        assert text is not None
        assert [(item.type, item.data) for item in text] == [("text", "画好了")]
        assert await _sent(MessageSegment.at("180910673"), "direct") is None

    asyncio.run(_run())


def test_group_send_keeps_at(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_send_path(monkeypatch)

    async def _run() -> None:
        content = await _sent(
            [MessageSegment.at("180910673"), MessageSegment.text("画好了")],
            "group",
        )
        assert content is not None
        assert [(item.type, item.data) for item in content] == [
            ("at", "180910673"),
            ("text", "画好了"),
        ]

    asyncio.run(_run())


def test_direct_forward_drops_nested_at(monkeypatch: pytest.MonkeyPatch) -> None:
    _quiet_send_path(monkeypatch)

    async def _run() -> None:
        content = await _sent(
            MessageSegment.node(
                [
                    MessageSegment.at("180910673"),
                    MessageSegment.text("画好了"),
                ]
            ),
            "direct",
        )
        assert content is not None
        assert len(content) == 1
        node = content[0]
        assert node.type == "node"
        data = node.data
        assert isinstance(data, list)
        inner = [item for item in data if isinstance(item, Message)]
        assert [(item.type, item.data) for item in inner] == [("text", "画好了")]
        assert await _sent(MessageSegment.node([MessageSegment.at("180910673")]), "direct") is None

    asyncio.run(_run())
