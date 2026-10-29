"""Mermaid 出图：mermaid.ink 的 SVG 能被 pytakumi 画出来；服务挂了只换掉这一块。"""

from __future__ import annotations

import base64
import asyncio
from io import BytesIO

import httpx
import pytest
from PIL import Image

from gsuid_core.utils.html_render import render_md_to_bytes, render_html_to_bytes
from gsuid_core.utils.html_render.mermaid_ink import (
    MermaidInkError,
    fetch_mermaid_svg,
    expand_mermaid_in_html,
)
from gsuid_core.utils.html_render.svg_chart_rewrite import rewrite_svg_charts_for_takumi

_SVG = """<svg id="mermaid-svg" xmlns="http://www.w3.org/2000/svg" width="100%"
 style="max-width:120px" viewBox="0 0 120 80">
<style>#mermaid-svg .node rect{fill:#ECECFF;stroke:#9370DB;stroke-width:2px;}</style>
<g class="nodes"><g class="node" transform="translate(60,40)">
<rect class="basic label-container" x="-40" y="-20" width="80" height="40"></rect>
<g class="label" transform="translate(-10,-10)"><foreignObject width="20" height="20">
<div xmlns="http://www.w3.org/1999/xhtml"><span class="nodeLabel"><p>图</p></span></div>
</foreignObject></g></g></g></svg>"""

_PNG = b"\x89PNG\r\n\x1a\n"


def _fake_svg(code: str, *, client: httpx.Client | None = None) -> str:
    del code, client
    return _SVG


def _down(code: str, *, client: httpx.Client | None = None) -> str:
    del code, client
    raise httpx.ConnectError("connection refused")


def _paint_counts(png: bytes) -> tuple[dict[str, int], tuple[int, int]]:
    image = Image.open(BytesIO(png)).convert("RGB")
    raw = image.tobytes()
    counts = {"red": 0, "blue": 0, "green": 0, "black": 0, "lavender": 0, "ink": 0}
    for index in range(0, len(raw), 3):
        red, green, blue = raw[index], raw[index + 1], raw[index + 2]
        if red >= 180 and red > green + 40 and red > blue + 40:
            counts["red"] += 1
        if blue >= 180 and blue > red + 40 and blue > green + 20:
            counts["blue"] += 1
        if green >= 120 and green > red + 40 and green > blue + 20:
            counts["green"] += 1
        if red <= 20 and green <= 20 and blue <= 20:
            counts["black"] += 1
        if abs(red - 236) <= 12 and abs(green - 236) <= 12 and abs(blue - 255) <= 8:
            counts["lavender"] += 1
        if red < 80 and green < 80 and blue < 80:
            counts["ink"] += 1
    return counts, image.size


def test_fetch_puts_the_diagram_in_the_mermaid_ink_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        encoded = request.url.path.split("/svg/", 1)[1]
        pad = "=" * (-len(encoded) % 4)
        text = base64.urlsafe_b64decode(encoded + pad).decode("utf-8")
        seen.append(str(request.url))
        seen.append(text)
        return httpx.Response(200, text="<svg xmlns='http://www.w3.org/2000/svg'></svg>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    for code in ("graph TD; A-->B", "graph TD\n    开始-->结束"):
        svg = fetch_mermaid_svg(code, client=client)
        assert svg.startswith("<svg")
        assert seen[-1] == code
        assert seen[-2].startswith("https://mermaid.ink/svg/")


def test_fetch_status_distinguishes_outage_from_bad_diagram() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        encoded = request.url.path.split("/svg/", 1)[1]
        pad = "=" * (-len(encoded) % 4)
        text = base64.urlsafe_b64decode(encoded + pad).decode("utf-8")
        if text == "bad":
            return httpx.Response(400, text="Unknown diagram error")
        return httpx.Response(503, text="down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(MermaidInkError) as bad:
        fetch_mermaid_svg("bad", client=client)
    assert bad.value.unavailable is False
    with pytest.raises(MermaidInkError) as down:
        fetch_mermaid_svg("graph TD; A-->B", client=client)
    assert down.value.unavailable is True


def test_html_shapes_keep_neighbors_when_one_diagram_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(code: str, *, client: httpx.Client | None = None) -> str:
        del client
        seen.append(code)
        if code.startswith("sequenceDiagram"):
            raise httpx.ConnectError("connection refused")
        return _SVG

    monkeypatch.setattr("gsuid_core.utils.html_render.mermaid_ink.fetch_mermaid_svg", fake)
    plain = expand_mermaid_in_html("<p>没有图</p>")
    assert plain == "<p>没有图</p>"

    html = (
        "<p>页头</p>"
        '<div class="mermaid">graph TD; A--&gt;B</div>'
        '<pre><code class="language-mermaid">sequenceDiagram\nAlice->>Bob: Hi</code></pre>'
        "<p>页脚</p>\n"
        "```mermaid\npie title Pets\n```"
    )
    out = expand_mermaid_in_html(html)
    assert "页头" in out and "页脚" in out
    assert "graph TD" not in out
    assert "sequenceDiagram" not in out
    assert "pie title Pets" not in out
    assert "服务不可用" in out
    assert 'data-gs-mermaid="1"' in out
    assert "graph TD; A-->B" in seen
    assert any(code.startswith("sequenceDiagram") for code in seen)
    assert any(code.startswith("pie title") for code in seen)


def test_bad_syntax_uses_a_different_terminal_line(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(code: str, *, client: httpx.Client | None = None) -> str:
        del code, client
        raise MermaidInkError("HTTP 400", unavailable=False)

    monkeypatch.setattr("gsuid_core.utils.html_render.mermaid_ink.fetch_mermaid_svg", fake)
    out = expand_mermaid_in_html('<div class="mermaid">nope</div><p>还在</p>')
    assert "这张图无法渲染" in out
    assert "服务不可用" not in out
    assert "还在" in out


def test_outage_still_renders_the_rest_of_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gsuid_core.utils.html_render.mermaid_ink.fetch_mermaid_svg", _down)
    html = (
        '<div style="background:#ffffff;padding:8px;">'
        '<div style="background:#2255ee;height:36px;"></div>'
        '<div class="mermaid">graph TD; A-->B</div>'
        '<div style="background:#1f9d55;height:36px;"></div>'
        "</div>"
    )
    expanded = expand_mermaid_in_html(html)
    assert "服务不可用" in expanded
    assert "graph TD" not in expanded
    png = asyncio.run(render_html_to_bytes(html, max_width=480, dpi=96, default_font_size=16))
    assert png.startswith(_PNG)
    counts, size = _paint_counts(png)
    assert counts["red"] > 40, counts
    assert counts["black"] > 500, counts
    assert counts["blue"] > 200, (counts, size)
    assert counts["green"] > 200, (counts, size)


def test_mermaid_svg_is_painted_at_its_own_size(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("gsuid_core.utils.html_render.mermaid_ink.fetch_mermaid_svg", _fake_svg)
    html = (
        '<div style="background:#f4f6f8;padding:8px;color:#222;font-size:16px;">'
        "<p>页眉</p>"
        '<pre class="mermaid">graph TD; A-->B</pre>'
        "</div>"
    )
    png = asyncio.run(render_html_to_bytes(html, max_width=480, dpi=96, default_font_size=16))
    assert png.startswith(_PNG)
    counts, size = _paint_counts(png)
    assert counts["lavender"] > 200, (counts, size)
    assert counts["ink"] > 10, (counts, size)
    # 拉成通栏时 120x80 的图会变成大约 320px 高
    assert size[1] < 320, size


def test_markdown_fence_is_painted(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(code: str, *, client: httpx.Client | None = None) -> str:
        del client
        seen.append(code)
        return _SVG

    monkeypatch.setattr("gsuid_core.utils.html_render.mermaid_ink.fetch_mermaid_svg", fake)
    md = "前文\n\n```mermaid\ngraph TD\n    A-->B\n```\n\n后文\n"
    png = asyncio.run(render_md_to_bytes(md=md, max_width=640, dpi=96))
    assert png.startswith(_PNG)
    assert seen and "graph TD" in seen[0] and "A-->B" in seen[0]
    counts, size = _paint_counts(png)
    assert counts["lavender"] > 200, (counts, size)


def test_chart_rewrite_keeps_mermaid_text_and_still_lifts_chart_text() -> None:
    chart = (
        '<svg viewBox="0 0 100 40" width="100" height="40">'
        '<rect width="10" height="10" fill="#112233"/>'
        '<text x="4" y="20">柱</text></svg>'
    )
    mermaid = '<svg data-gs-mermaid="1" width="80" height="40" viewBox="0 0 80 40"><text x="4" y="20">图</text></svg>'
    out = rewrite_svg_charts_for_takumi(chart + mermaid)
    assert "svg-label" in out
    assert "柱" in out
    assert "图</text>" in out
    assert 'data-gs-mermaid="1"' in out
