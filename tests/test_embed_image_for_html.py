"""HTML 自动嵌图：data URI / icon 语法 / 收集 src / 管线替换。"""

from __future__ import annotations

import base64
import asyncio
from unittest.mock import AsyncMock, patch


def test_sniff_and_data_uri_roundtrip() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import (
        _sniff_image_mime,
        _bytes_to_data_uri,
    )

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    assert _sniff_image_mime(png) == "image/png"
    uri = _bytes_to_data_uri(png)
    assert uri.startswith("data:image/png;base64,")


def test_sniff_svg() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _sniff_image_mime

    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle cx="1" cy="1" r="1"/></svg>'
    assert _sniff_image_mime(svg) == "image/svg+xml"


def test_icon_source_regex() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _ICON_SOURCE_RE

    m = _ICON_SOURCE_RE.match("icon:mdi/chart-line")
    assert m is not None
    assert m.group("prefix") == "mdi"
    assert m.group("name") == "chart-line"
    assert _ICON_SOURCE_RE.match("icon:simple-icons/tesla") is not None
    assert _ICON_SOURCE_RE.match("https://x.com/a.png") is None


def test_needs_auto_embed() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _needs_auto_embed

    assert _needs_auto_embed("https://cdn.example.com/a.png")
    assert _needs_auto_embed("icon:mdi/star")
    assert _needs_auto_embed("img_abc123")
    assert _needs_auto_embed("res_deadbeef")
    assert not _needs_auto_embed("data:image/png;base64,xxx")
    assert not _needs_auto_embed("./local.png")
    assert not _needs_auto_embed("")


def test_collect_auto_embed_sources() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _collect_auto_embed_sources

    html = """
    <img src="https://a.example/x.png">
    <img src='icon:mdi/chart-line' alt="c">
    <img src="data:image/png;base64,AAA">
    <div style="background-image:url(https://a.example/x.png)"></div>
    <div style="background:url(icon:mdi/star)"></div>
    """
    srcs = _collect_auto_embed_sources(html)
    assert "https://a.example/x.png" in srcs
    assert "icon:mdi/chart-line" in srcs
    assert "icon:mdi/star" in srcs
    # 去重：同一 https 只一次
    assert srcs.count("https://a.example/x.png") == 1
    assert all(not s.startswith("data:") for s in srcs)


def test_resolve_data_uri_source() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import (
        _bytes_to_data_uri,
        _resolve_embed_source_bytes,
    )

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    src = f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"

    async def _run() -> None:
        data, mime, err = await _resolve_embed_source_bytes(src)
        assert err is None and data is not None
        assert mime == "image/png"
        assert _bytes_to_data_uri(data, mime).startswith("data:image/png;base64,")

    asyncio.run(_run())


def test_resolve_empty_source() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _resolve_embed_source_bytes

    async def _run() -> None:
        data, mime, err = await _resolve_embed_source_bytes("  ")
        assert data is None and err is not None

    asyncio.run(_run())


def test_auto_embed_replaces_img_and_css_url() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import (
        _TRANSPARENT_1PX_PNG,
        _auto_embed_html_images,
    )

    fake_uri = "data:image/svg+xml;base64,PHN2Zz4="

    async def _fake_to_uri(source: str, *, max_side: int = 512):
        if source.startswith("icon:"):
            return fake_uri, None
        if source.startswith("https://"):
            return fake_uri, None
        return None, "boom"

    html = (
        '<img src="icon:mdi/star" class="i">'
        '<div style="background-image:url(https://cdn.example/a.png)"></div>'
        '<img src="data:image/png;base64,keepme">'
    )

    async def _run() -> None:
        with patch(
            "gsuid_core.ai_core.buildin_tools.html_render_tools._source_to_data_uri",
            new=AsyncMock(side_effect=_fake_to_uri),
        ):
            out = await _auto_embed_html_images(html)
        assert 'src="icon:mdi/star"' not in out
        assert fake_uri in out
        assert "https://cdn.example/a.png" not in out
        assert "data:image/png;base64,keepme" in out
        assert _TRANSPARENT_1PX_PNG  # 常量仍可用

    asyncio.run(_run())


def test_auto_embed_fail_uses_transparent_placeholder() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import (
        _TRANSPARENT_1PX_PNG,
        _auto_embed_html_images,
    )

    async def _run() -> None:
        with patch(
            "gsuid_core.ai_core.buildin_tools.html_render_tools._source_to_data_uri",
            new=AsyncMock(return_value=(None, "down")),
        ):
            out = await _auto_embed_html_images('<img src="https://bad.example/x.png">')
        assert _TRANSPARENT_1PX_PNG in out
        assert "https://bad.example/x.png" not in out

    asyncio.run(_run())


def test_prepare_injects_engine_hygiene() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _prepare_free_html

    out = _prepare_free_html("<div class='tag'>标签</div>")
    assert "engine hygiene" in out
    assert "white-space:nowrap" in out


def test_embed_image_tool_not_registered() -> None:
    """嵌图已并入 render 管线，不再作为独立 agent 工具。"""
    from gsuid_core.ai_core.register import get_all_tools

    names = set(get_all_tools().keys()) if callable(get_all_tools) else set()
    # 兼容不同 register API
    try:
        from gsuid_core.ai_core.register import get_registered_tools

        reg = get_registered_tools()
        flat = set()
        for cat in reg.values():
            flat.update(cat.keys())
        names = flat
    except Exception:
        pass
    assert "embed_image_for_html" not in names
