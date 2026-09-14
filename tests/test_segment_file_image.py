"""image 段 file:// 透传：不读盘、不转 base64/link。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from gsuid_core.models import Message
from gsuid_core.segment import MessageSegment, to_markdown, convert_message


def test_messagesegment_image_keeps_file_uri() -> None:
    uri = "file:///C:/images/a.jpg"
    msg = MessageSegment.image(uri)
    assert msg.type == "image"
    assert msg.data == uri


def test_messagesegment_image_does_not_open_missing_file_uri() -> None:
    uri = "file:///definitely-not-a-real-path/nope.jpg"
    msg = MessageSegment.image(uri)
    assert msg.data == uri


def test_convert_message_keeps_file_uri() -> None:
    uri = "file:///home/bot/a.jpg"

    async def _run() -> None:
        out = await convert_message(Message(type="image", data=uri), "onebot", "123")
        assert len(out) == 1
        assert out[0].type == "image"
        assert out[0].data == uri

    asyncio.run(_run())


def test_convert_message_string_file_uri_is_image() -> None:
    uri = "file://localhost/tmp/a.jpg"

    async def _run() -> None:
        out = await convert_message(uri, "onebot", "123")
        assert len(out) == 1
        assert out[0].type == "image"
        assert out[0].data == uri

    asyncio.run(_run())


def test_convert_node_keeps_file_uri() -> None:
    uri = "file:///tmp/node.jpg"

    async def _run() -> None:
        node = Message(type="node", data=[Message(type="image", data=uri)])
        out = await convert_message(node, "onebot", "123")
        assert len(out) == 1
        assert out[0].type == "node"
        inner = out[0].data
        assert isinstance(inner, list)
        assert inner[0].type == "image"
        assert inner[0].data == uri

    asyncio.run(_run())


def test_to_markdown_keeps_file_uri() -> None:
    uri = "file:///tmp/md.jpg"

    async def _run() -> None:
        out = await to_markdown([Message(type="image", data=uri)], buttons=[], bot_id="onebot")
        images = [m for m in out if m.type == "image"]
        assert len(images) == 1
        assert images[0].data == uri

    asyncio.run(_run())


def test_messagesegment_image_path_still_encodes(tmp_path: Path) -> None:
    p = tmp_path / "a.png"
    p.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
        b"\x00\x01\x01\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    msg = MessageSegment.image(p)
    assert msg.type == "image"
    assert isinstance(msg.data, str)
    assert msg.data.startswith("base64://")
