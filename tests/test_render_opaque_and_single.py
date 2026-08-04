"""render 出图：不透明合成 + 同任务单次推送。"""

from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

from PIL import Image
from pydantic_ai import RunContext

from gsuid_core.ai_core.planning.runtime import PlanRunContext


def _rgba_png_with_hole() -> bytes:
    """半透明 PNG：中心不透明，外围透明。"""
    im = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    for x in range(10, 30):
        for y in range(10, 30):
            im.putpixel((x, y), (30, 80, 160, 255))
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _opaque_rgb_png() -> bytes:
    im = Image.new("RGB", (20, 20), (255, 0, 0))
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_ensure_opaque_flattens_alpha() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _ensure_opaque_image_bytes

    raw = _rgba_png_with_hole()
    out = _ensure_opaque_image_bytes(raw)
    im = Image.open(BytesIO(out))
    im.load()
    assert im.mode == "RGB"
    corner = im.getpixel((0, 0))
    assert isinstance(corner, tuple)
    assert len(corner) >= 3
    r, g, b = int(corner[0]), int(corner[1]), int(corner[2])
    # 原透明角点应被实色填充
    assert r + g + b > 0 or (r, g, b) == (15, 23, 42)


def test_ensure_opaque_noop_on_rgb() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _ensure_opaque_image_bytes

    raw = _opaque_rgb_png()
    out = _ensure_opaque_image_bytes(raw)
    assert out == raw


def test_finish_image_single_emit_per_task() -> None:
    from gsuid_core.ai_core.buildin_tools import html_render_tools as hr

    hr._RENDER_EMITTED_TASKS.clear()
    png = _opaque_rgb_png()
    ctx = cast(RunContext[Any], SimpleNamespace(deps=SimpleNamespace(bot=object())))
    plan = PlanRunContext(task_id="task_single_1", root_task_id="root_1")

    async def _run() -> tuple[str | bytes, str | bytes]:
        with (
            patch.object(hr, "_try_send_image", new=AsyncMock(return_value=True)),
            patch(
                "gsuid_core.ai_core.planning.runtime.get_plan_context",
                return_value=plan,
            ),
        ):
            first = await hr._finish_image(ctx, png)
            second = await hr._finish_image(ctx, png)
            return first, second

    first, second = asyncio.run(_run())
    assert isinstance(first, str)
    assert "图片已发送" in first or "KB" in first
    assert isinstance(second, str)
    assert "已成功出过图" in second
    hr._RENDER_EMITTED_TASKS.clear()
