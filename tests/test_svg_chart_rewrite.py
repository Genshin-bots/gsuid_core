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


def test_rewrite_idempotent_when_no_text() -> None:
    svg = '<svg width="10" height="10"><rect x="0" y="0" width="10" height="10" fill="#000"/></svg>'
    assert rewrite_svg_charts_for_takumi(svg) == svg


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
    assert "left:50.0px" in out
    assert "top:" in out


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


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param
