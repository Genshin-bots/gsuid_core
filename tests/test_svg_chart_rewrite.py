"""SVG <text> → HTML 覆盖层：结构断言 + 像素级标题可见。"""

from __future__ import annotations

from pathlib import Path

import pytest

from gsuid_core.utils.html_render.svg_chart_rewrite import rewrite_svg_charts_for_takumi

_LABEL = "CHARTLABELZX"
_SVG = (
    '<svg width="400" height="200" viewBox="0 0 400 200">'
    '<rect x="0" y="0" width="400" height="200" fill="#ffffff"/>'
    '<rect x="40" y="90" width="80" height="70" fill="#38bdf8"/>'
    f'<text x="200" y="36" font-size="28" fill="#111111" text-anchor="middle">{_LABEL}</text>'
    "</svg>"
)


def test_rewrite_lifts_svg_text_to_span() -> None:
    html = f"<div>{_SVG}</div>"
    out = rewrite_svg_charts_for_takumi(html)
    assert 'class="svg-wrap"' in out
    assert "svg-label" in out
    assert _LABEL in out
    assert f">{_LABEL}</text>" not in out.replace(" ", "")
    assert "<rect" in out
    assert "width:100%" in out
    assert "width:400px" not in out
    assert "left:50.00%" in out


def test_rewrite_idempotent_when_no_text() -> None:
    svg = '<svg width="10" height="10"><rect x="0" y="0" width="10" height="10" fill="#000"/></svg>'
    once = rewrite_svg_charts_for_takumi(svg)
    assert 'class="svg-wrap"' in once
    assert 'width="100%"' in once
    assert rewrite_svg_charts_for_takumi(once) == once


def test_rewrite_passthrough_without_svg() -> None:
    html = "<p>hello</p>"
    assert rewrite_svg_charts_for_takumi(html) == html


def test_rewrite_accumulates_ancestor_translate() -> None:
    svg = (
        '<svg width="200" height="80">'
        '<g transform="translate(40, 20)">'
        '<text x="10" y="12" font-size="14" fill="#111111">NESTLBL</text>'
        "</g></svg>"
    )
    out = rewrite_svg_charts_for_takumi(svg)
    assert "NESTLBL" in out
    assert "left:25.00%" in out
    assert "top:" in out
    assert "left:50.0px" not in out


@pytest.mark.anyio
async def test_svg_text_visible_after_render(tmp_path: Path) -> None:
    pytakumi = pytest.importorskip("pytakumi")
    _ = pytakumi
    import io

    from PIL import Image

    from gsuid_core.utils.html_render import render_html_to_bytes

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:#ffffff;}}
</style></head><body>{_SVG}</body></html>"""
    rewritten = rewrite_svg_charts_for_takumi(html)
    assert _LABEL in rewritten
    data = await render_html_to_bytes(
        rewritten,
        max_width=400,
        dpi=96,
        default_font_size=16,
        image_format="png",
    )
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    out_dir = Path(__file__).resolve().parent / "test_output"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "svg_text_overlay.png").write_bytes(data)
    im = Image.open(io.BytesIO(data)).convert("L")
    # 标题带在图上方：取顶部 70px 带，深色像素必须明显多于空白图
    band = im.crop((0, 0, im.width, min(70, im.height)))
    dark = sum(1 for p in band.getdata() if p < 180)
    assert dark > 80, f"标题带深色像素过少 ({dark})，SVG 文字仍可能丢失"


@pytest.mark.anyio
async def test_wide_chart_fits_two_column_card() -> None:
    """680px 图放进 2 栏卡片不得撑破红框。"""
    pytakumi = pytest.importorskip("pytakumi")
    _ = pytakumi
    import io

    from PIL import Image

    from gsuid_core.utils.html_render import render_html_to_bytes

    wide = (
        '<svg width="680" height="260" viewBox="0 0 680 260">'
        '<rect x="0" y="0" width="680" height="260" fill="#e8f0fe"/>'
        '<rect x="40" y="40" width="600" height="180" fill="#3b82f6"/>'
        f'<text x="340" y="30" font-size="18" fill="#111111" text-anchor="middle">{_LABEL}</text>'
        "</svg>"
    )
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:#ffffff;}}
.page{{width:800px;padding:20px;box-sizing:border-box;}}
.card{{background:#ffffff;border:4px solid #ef4444;padding:16px;box-sizing:border-box;}}
.grid{{display:flex;gap:12px;}}
.cell{{flex:1;background:#f8fafc;}}
</style></head><body><div class="page"><div class="card">
<div class="grid"><div class="cell">{wide}</div><div class="cell">{wide}</div></div>
</div></div></body></html>"""
    rewritten = rewrite_svg_charts_for_takumi(html)
    assert "width:680px" not in rewritten
    assert 'width="100%"' in rewritten
    data = await render_html_to_bytes(
        rewritten,
        max_width=800,
        dpi=96,
        default_font_size=16,
        image_format="png",
        root_max_width=800,
    )
    im = Image.open(io.BytesIO(data)).convert("RGB")
    assert im.size[0] == 800
    # 右缘应是页底白/卡片红，不能是图表蓝（越框）
    edge_x = im.size[0] - 3
    blues = 0
    for y in range(im.size[1]):
        pixel = im.getpixel((edge_x, y))
        if not isinstance(pixel, tuple) or len(pixel) < 3:
            continue
        r, g, b = pixel[0], pixel[1], pixel[2]
        if not isinstance(r, int) or not isinstance(g, int) or not isinstance(b, int):
            continue
        if b > 180 and r < 100 and g < 180:
            blues += 1
    assert blues < 8, f"右缘出现图表蓝 {blues}px，图表仍越出卡片"
    out_dir = Path(__file__).resolve().parent / "test_output"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "svg_two_col_fit.png").write_bytes(data)


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param
