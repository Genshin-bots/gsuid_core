"""表情入库短边 156，发出前和文字用同一条间隔。"""

from __future__ import annotations

import io
import asyncio
from types import SimpleNamespace
from pathlib import Path

from PIL import Image
from pytest import MonkeyPatch

from gsuid_core.bot import Bot, _Bot
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import MEME_PAUSE_CHARS, bubble_pause
from gsuid_core.ai_core.meme.normalize import SHORT_SIDE, prepare_meme_image


def _png(size: tuple[int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, (20, 40, 60, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _gif(size: tuple[int, int], frames: int = 2) -> bytes:
    images = [Image.new("RGBA", size, (index * 40, 10, 20, 255)) for index in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, format="GIF", save_all=True, append_images=images[1:], duration=80, loop=0)
    return buf.getvalue()


def _frame_count(data: bytes) -> int:
    image = Image.open(io.BytesIO(data))
    count = 1
    try:
        while True:
            image.seek(count)
            count += 1
    except EOFError:
        return count


def test_short_side_becomes_156() -> None:
    prepared = prepare_meme_image(_png((400, 100)), "image/png")
    assert prepared.mime == "image/png"
    assert prepared.width == 624
    assert prepared.height == SHORT_SIDE
    with Image.open(io.BytesIO(prepared.data)) as image:
        assert image.size == (624, SHORT_SIDE)


def test_smaller_image_scales_up() -> None:
    prepared = prepare_meme_image(_png((80, 80)), "image/png")
    assert (prepared.width, prepared.height) == (SHORT_SIDE, SHORT_SIDE)


def test_already_156_keeps_bytes() -> None:
    raw = _png((SHORT_SIDE, 200))
    prepared = prepare_meme_image(raw, "image/png")
    assert prepared.data == raw
    assert prepared.mime == "image/png"


def test_gif_keeps_every_frame() -> None:
    prepared = prepare_meme_image(_gif((300, 200), frames=3), "image/gif")
    assert prepared.mime == "image/gif"
    assert min(prepared.width, prepared.height) == SHORT_SIDE
    assert _frame_count(prepared.data) == 3


def test_bubble_pause_uses_the_text_curve() -> None:
    assert bubble_pause(0) == 0.5
    assert bubble_pause(MEME_PAUSE_CHARS) == 1.0
    assert bubble_pause(21) == 3.0
    assert bubble_pause(100) == 3.0


def _ev() -> Event:
    return Event(
        bot_id="onebot",
        bot_self_id="self1",
        user_type="group",
        group_id="g1",
        user_id="u1",
        WS_BOT_ID="ws1",
        sender={"nickname": "甲"},
    )


def test_meme_tag_waits_one_second_before_send(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    from gsuid_core.ai_core.utils import _send_meme_from_tag

    order: list[str] = []

    async def _sleep(seconds: float) -> None:
        order.append(f"sleep:{seconds}")

    async def _send(_msg: object, extra_metadata: object = None, **_k: object) -> None:
        order.append("send")

    async def _pick(**_k: object) -> tuple[object, str]:
        return SimpleNamespace(file_path="x.png", meme_id="m1", description="d"), ""

    async def _read(_p: object) -> bytes:
        return b"img"

    async def _usage(_mid: object, _gid: object) -> None:
        return None

    async def _convert(_data: object) -> str:
        return "base64://YQ=="

    png = tmp_path / "x.png"
    png.write_bytes(b"img")
    ev = _ev()
    bot = Bot(_Bot("meme-test"), ev)
    monkeypatch.setattr(bot, "send", _send)
    monkeypatch.setattr("gsuid_core.ai_core.utils.asyncio.sleep", _sleep)
    monkeypatch.setattr("gsuid_core.ai_core.persona.config.persona_allows_meme_reply", lambda _name: True)
    monkeypatch.setattr("gsuid_core.ai_core.meme.selector.pick", _pick)
    monkeypatch.setattr("gsuid_core.ai_core.meme.library._read_file", _read)
    monkeypatch.setattr("gsuid_core.ai_core.meme.library.get_memes_base_path", lambda: tmp_path)
    monkeypatch.setattr("gsuid_core.ai_core.meme.database_model.AiMemeRecord.record_usage", _usage)
    monkeypatch.setattr("gsuid_core.ai_core.utils.convert_img", _convert)
    monkeypatch.setattr(
        "gsuid_core.ai_core.buildin_tools.meme_tools._get_persona_for_event",
        lambda _ev: "早柚",
    )

    asyncio.run(_send_meme_from_tag("开心", bot, ev))
    assert order == ["sleep:1.0", "send"]


def test_meme_tag_splits_mood_and_scene(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    from gsuid_core.ai_core.utils import _send_meme_from_tag

    picked: dict[str, object] = {}

    async def _sleep(_seconds: float) -> None:
        return None

    async def _send(_msg: object, extra_metadata: object = None, **_k: object) -> None:
        return None

    async def _pick(**kwargs: object) -> tuple[object, str]:
        picked.update(kwargs)
        return SimpleNamespace(file_path="x.png", meme_id="m1", description="d"), ""

    async def _read(_p: object) -> bytes:
        return b"img"

    async def _usage(_mid: object, _gid: object) -> None:
        return None

    async def _convert(_data: object) -> str:
        return "base64://YQ=="

    png = tmp_path / "x.png"
    png.write_bytes(b"img")
    ev = _ev()
    bot = Bot(_Bot("meme-test"), ev)
    monkeypatch.setattr(bot, "send", _send)
    monkeypatch.setattr("gsuid_core.ai_core.utils.asyncio.sleep", _sleep)
    monkeypatch.setattr("gsuid_core.ai_core.persona.config.persona_allows_meme_reply", lambda _name: True)
    monkeypatch.setattr("gsuid_core.ai_core.meme.selector.pick", _pick)
    monkeypatch.setattr("gsuid_core.ai_core.meme.library._read_file", _read)
    monkeypatch.setattr("gsuid_core.ai_core.meme.library.get_memes_base_path", lambda: tmp_path)
    monkeypatch.setattr("gsuid_core.ai_core.meme.database_model.AiMemeRecord.record_usage", _usage)
    monkeypatch.setattr("gsuid_core.ai_core.utils.convert_img", _convert)
    monkeypatch.setattr(
        "gsuid_core.ai_core.buildin_tools.meme_tools._get_persona_for_event",
        lambda _ev: "早柚",
    )

    asyncio.run(_send_meme_from_tag("害羞，卖萌", bot, ev))
    assert picked["mood"] == "害羞"
    assert picked["scene"] == "卖萌"
    picked.clear()
    asyncio.run(_send_meme_from_tag("害羞,卖萌", bot, ev))
    assert picked["mood"] == "害羞"
    assert picked["scene"] == "卖萌"
