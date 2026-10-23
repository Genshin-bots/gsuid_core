"""render_agent 配图链路：360 解析、图片规整、插件图校验、组件库注入、图标着色、嵌图清晰度。"""

from __future__ import annotations

import re
import json
import base64
import asyncio
from io import BytesIO

import pytest
from PIL import Image

from gsuid_core.ai_core.buildin_tools import html_render_tools as hrt
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.buildin_tools.render_kit import RENDER_KIT_MARK
from gsuid_core.ai_core.buildin_tools.image_finder import (
    SOURCE_FIRECRAWL,
    normalize_image,
    parse_so360_payload,
    plugin_image_matches,
    remote_image_candidate,
)


def _png(width: int, height: int, *, alpha: bool = False) -> bytes:
    mode, color = ("RGBA", (10, 120, 200, 0)) if alpha else ("RGB", (10, 120, 200))
    buf = BytesIO()
    Image.new(mode, (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def test_so360_payload_falls_back_to_thumb_and_skips_bad_items() -> None:
    payload = {
        "list": [
            {
                "img": "https://a/1.jpg",
                "thumb": "https://a/1s.jpg",
                "width": "1200",
                "height": "800",
                "title": "<em>神仙居</em>",
            },
            {"img": "", "thumb": "https://a/2s.jpg", "title": "只有缩略图"},
            {"img": "https://a/3.jpg", "width": 120, "height": 90, "title": "太小"},
            {"img": "https://a/4.jpg", "width": 3000, "height": 400, "title": "太扁"},
            "not-a-dict",
        ]
    }
    got = parse_so360_payload(json.dumps(payload), limit=10)

    assert [(c.url, c.fallback_url, c.title) for c in got] == [
        ("https://a/1.jpg", "https://a/1s.jpg", "神仙居"),
        ("https://a/2s.jpg", "", "只有缩略图"),
    ]
    assert len(parse_so360_payload(json.dumps(payload), limit=1)) == 1
    assert parse_so360_payload("<html>forbid</html>", limit=5) == []


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (_png(3000, 2000), ("JPEG", 1280)),
        (_png(900, 900, alpha=True), ("PNG", 900)),
        (_png(100, 100), None),
        (_png(3000, 500), None),
        (b"<html>403</html>", None),
    ],
    ids=["large-opaque", "transparent", "tiny", "panorama", "not-an-image"],
)
def test_normalize_image(data: bytes, expected: tuple[str, int] | None) -> None:
    norm = normalize_image(data)
    if expected is None:
        assert norm is None
        return
    assert norm is not None
    im = Image.open(BytesIO(norm["data"]))
    assert (im.format, max(im.size)) == expected
    assert (norm["width"], norm["height"]) == im.size


def test_remote_image_candidate_skips_known_bad_size() -> None:
    ok = remote_image_candidate("https://a/1.jpg", "神仙居", 1200, 800, SOURCE_FIRECRAWL)
    unknown = remote_image_candidate("https://a/2.jpg", "", 0, 0, SOURCE_FIRECRAWL)
    tiny = remote_image_candidate("https://a/3.jpg", "太小", 120, 90, SOURCE_FIRECRAWL)

    assert ok is not None and ok.source == SOURCE_FIRECRAWL and ok.url == "https://a/1.jpg"
    assert unknown is not None and unknown.url == "https://a/2.jpg"
    assert tiny is None
    assert remote_image_candidate("not-a-url", "x", 800, 600, SOURCE_FIRECRAWL) is None


def _page(classes: str, body: str = "<div class='gs-band'></div>") -> str:
    return f"<div class='{classes}'>{body}</div>"


@pytest.mark.parametrize(
    ("html", "embed_on", "searched", "found", "expect"),
    [
        (_page("gs-page layout-feature"), False, False, False, None),
        (_page("gs-page layout-feature", '<img src="img_abc123">'), True, True, True, None),
        (_page("gs-page layout-dashboard theme-slate"), True, False, False, None),
        (_page("theme-ink layout-ranking gs-page"), True, True, True, None),
        (_page("gs-page layout-feature"), True, False, False, "find_images"),
        (_page("gs-page layout-gallery"), True, True, True, "句柄"),
        (_page("gs-page layout-magazine"), True, True, False, None),
        (_page("gs-page theme-ocean"), True, False, False, "layout-"),
        (_page("gs-page layout-poster"), True, False, False, "layout-"),
    ],
    ids=[
        "embed-off",
        "has-photo",
        "data-layout-needs-no-photo",
        "class-order-free",
        "photo-layout-not-searched",
        "photo-layout-unused-handles",
        "photo-layout-nothing-found",
        "undeclared-acts-as-photo",
        "unknown-layout-acts-as-undeclared",
    ],
)
def test_photo_gate_follows_layout(html: str, embed_on: bool, searched: bool, found: bool, expect: str | None) -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import embed_photo_block_reason

    got = embed_photo_block_reason(html, embed_on=embed_on, searched=searched, found=found)
    if expect is None:
        assert got is None
    else:
        assert got is not None and expect in got


def test_embed_images_default_on() -> None:
    from gsuid_core.ai_core.configs.ai_config import render_embed_images_enabled

    assert render_embed_images_enabled() is True


def test_render_prompt_teaches_only_known_layouts_and_classes() -> None:
    from gsuid_core.ai_core.buildin_tools.render_kit import RENDER_KIT_CSS, RENDER_LAYOUTS
    from gsuid_core.ai_core.capability_agents.profiles import _RENDER_PROMPT

    taught = set(re.findall(r"\blayout-([a-z]+)\b", _RENDER_PROMPT)) - {"xxx"}
    assert taught == set(RENDER_LAYOUTS)
    defined = set(re.findall(r"\.(gs-[a-z0-9-]+)", RENDER_KIT_CSS))
    used = set(re.findall(r"(?<![\w-])(gs-[a-z][a-z0-9-]*)", _RENDER_PROMPT))
    assert used - defined == set()


def test_plugin_image_must_name_the_queried_object() -> None:
    assert plugin_image_matches("原神 芙宁娜", "芙宁娜 立绘 GenshinUID")
    assert not plugin_image_matches("原神 芙宁娜", "那维莱特 立绘 原神")
    assert plugin_image_matches("芙宁娜", "Furina 芙宁娜")
    assert not plugin_image_matches("   ", "芙宁娜")


def test_render_kit_lands_before_agent_style() -> None:
    html = '<style>.gs-title{color:red}</style><div class="gs-page"><div class="gs-title">T</div></div>'
    out = hrt._prepare_free_html(html)

    assert out.index(RENDER_KIT_MARK) < out.index(".gs-title{color:red}")
    assert hrt._prepare_free_html(out).count(RENDER_KIT_MARK) == 1
    assert RENDER_KIT_MARK not in hrt._prepare_free_html('<div class="card">plain</div>')


def test_icon_color_is_sent_to_iconify(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    async def fake_download(url: str) -> tuple[bytes | None, str | None]:
        requested.append(url)
        return b"<svg xmlns='http://www.w3.org/2000/svg'/>", None

    monkeypatch.setattr(hrt, "_download_url_bytes", fake_download)
    _, mime, err = asyncio.run(hrt._resolve_embed_source_bytes("icon:mdi/map-marker?color=1D6FB8"))
    asyncio.run(hrt._resolve_embed_source_bytes("icon:mdi/map-marker"))

    assert (mime, err) == ("image/svg+xml", None)
    assert requested[0].endswith("/mdi/map-marker.svg?color=%231d6fb8")
    assert "?" not in requested[1]


def test_embedded_photo_stays_sharp_at_two_x() -> None:
    rid = RM.register(_png(2400, 1600))
    out = asyncio.run(hrt._auto_embed_html_images(f'<div><img src="{rid}"></div>'))
    match = re.search(r'src="data:(image/[a-z+]+);base64,([^"]+)"', out)

    assert match is not None
    im = Image.open(BytesIO(base64.b64decode(match.group(2))))
    # 600 逻辑宽 × 2 倍像素比：满宽头图低于 1200px 会糊
    assert max(im.size) >= 1200
    assert (match.group(1), im.format) == ("image/jpeg", "JPEG")
