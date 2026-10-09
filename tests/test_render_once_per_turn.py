"""无看板任务的出图去重按本轮 turn_id，失败和只回传字节都不占名额。"""

import asyncio
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from PIL import Image

from gsuid_core.models import Event
from gsuid_core.ai_core.buildin_tools import html_render_tools as hr


def _png() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class _SendBot:
    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.sends = 0

    async def send(self, msg: object, extra_metadata: object = None, **_k: object) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("send down")
        self.sends += 1


def _ctx(turn_id: str, bot: _SendBot, *, allow_outbound: bool) -> MagicMock:
    ev = Event(
        bot_id="onebot",
        bot_self_id="1",
        msg_id="m-render",
        user_type="group",
        group_id="g-render-shared",
        user_id="user-a",
    )
    ctx = MagicMock()
    ctx.deps.allow_user_outbound = allow_outbound
    ctx.deps.bot = bot
    ctx.deps.extra = {"turn_id": turn_id}
    ctx.deps.ev = ev
    return ctx


def test_failed_send_does_not_stick_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次直发失败不占本轮名额；成功后同一轮不再出第二张，换一轮还可以。"""
    monkeypatch.setattr("gsuid_core.ai_core.planning.runtime.get_plan_context", lambda: None)
    bot = _SendBot(fail_times=1)
    turn = "turn-render-fail-then-ok"
    other = "turn-render-next"
    ctx = _ctx(turn, bot, allow_outbound=True)
    session_key = f"session:{ctx.deps.ev.session_id}"

    async def _go() -> tuple[object, object, object, object]:
        first = await hr._finish_image(ctx, _png())
        second = await hr._finish_image(ctx, _png())
        third = await hr._finish_image(ctx, _png())
        nxt = await hr._finish_image(_ctx(other, bot, allow_outbound=True), _png())
        return first, second, third, nxt

    try:
        first, second, third, nxt = asyncio.run(_go())
        assert isinstance(first, bytes)
        assert isinstance(second, str) and "图片已发送" in second
        assert isinstance(third, str) and "已成功出过图" in third
        assert isinstance(nxt, str) and "图片已发送" in nxt
        assert bot.sends == 2
        assert session_key not in hr._RENDER_EMITTED_TASKS
        assert f"turn:{turn}" in hr._RENDER_EMITTED_TASKS
    finally:
        hr._RENDER_EMITTED_TASKS.discard(f"turn:{turn}")
        hr._RENDER_EMITTED_TASKS.discard(f"turn:{other}")


def test_bytes_only_render_does_not_mark_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有出站权限、也没有句柄时，回传字节不能把这一轮记成已经出过图。"""
    monkeypatch.setattr("gsuid_core.ai_core.planning.runtime.get_plan_context", lambda: None)
    bot = _SendBot(fail_times=0)
    turn = "turn-render-bytes-only"
    ctx = _ctx(turn, bot, allow_outbound=False)

    async def _go() -> tuple[object, object]:
        return await hr._finish_image(ctx, _png()), await hr._finish_image(ctx, _png())

    try:
        first, second = asyncio.run(_go())
        assert isinstance(first, bytes)
        assert isinstance(second, bytes)
        assert bot.sends == 0
        assert f"turn:{turn}" not in hr._RENDER_EMITTED_TASKS
    finally:
        hr._RENDER_EMITTED_TASKS.discard(f"turn:{turn}")
