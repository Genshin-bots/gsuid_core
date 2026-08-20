"""把内联 SVG 的 ``<text>`` 提升为 HTML 覆盖层，供 pytakumi 绘制。

Takumi 对内联 ``<svg>`` 只实现形状元素，丢弃全部 ``<text>``。图表标注
（标题 / 类目 / 图例 / 数值）因此会整页消失。本改写器：

- 形状留在 SVG 里；
- 每个 ``<text>`` 按 x/y/font-size/fill/text-anchor 映射成绝对定位 ``<span>``。

只解析 SVG/HTML 结构属性，不按业务域分支。
"""

from __future__ import annotations

import re
from html import unescape

_SVG_BLOCK_RE = re.compile(r"<svg\b[^>]*>.*?</svg>", re.IGNORECASE | re.DOTALL)
_SVG_TEXT_RE = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"""([:\w.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
_TAG_RE = re.compile(r"<[^>]+>")
_VIEWBOX_RE = re.compile(r"viewBox\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
_TRANSLATE_RE = re.compile(r"translate\(\s*([-\d.]+)(?:[,\s]+([-\d.]+))?\s*\)", re.IGNORECASE)

_OVERLAY_CSS = (
    ".svg-wrap{position:relative;display:block;line-height:0;}"
    ".svg-wrap>svg{display:block;}"
    ".svg-label{position:absolute;line-height:1;white-space:nowrap;"
    'font-family:"MiSans","PingFang SC","Microsoft YaHei",sans-serif;'
    "pointer-events:none;}"
)


def _parse_attrs(attr_blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _ATTR_RE.finditer(attr_blob or ""):
        key = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4)
        out[key] = val or ""
    return out


def _num_attr(attrs: dict[str, str], name: str, default: float) -> float:
    raw = attrs[name] if name in attrs else ""
    if not raw:
        return default
    body = raw.strip().replace("px", "")
    if not body:
        return default
    if body[0] in "+-":
        sign = -1.0 if body[0] == "-" else 1.0
        body = body[1:]
    else:
        sign = 1.0
    if body.count(".") > 1:
        return default
    digits = body.replace(".", "")
    if not digits.isdigit():
        return default
    return sign * float(body)


def _dim_from_open(open_tag: str, name: str) -> float | None:
    m = re.search(rf"""(?:^|\s){name}\s*=\s*['"]?([\d.]+)""", open_tag, flags=re.I)
    if m is None:
        return None
    body = m.group(1)
    if body.count(".") > 1 or not body.replace(".", "").isdigit():
        return None
    return float(body)


def _svg_size(svg: str) -> tuple[float, float]:
    end = svg.find(">")
    open_tag = svg[: end + 1] if end != -1 else svg[:120]
    w = _dim_from_open(open_tag, "width")
    h = _dim_from_open(open_tag, "height")
    if w is not None and h is not None and w > 0 and h > 0:
        return w, h
    vm = _VIEWBOX_RE.search(open_tag)
    if vm is not None:
        parts = vm.group(1).replace(",", " ").split()
        if len(parts) == 4:
            bw, bh = parts[2], parts[3]
            if bw.replace(".", "").isdigit() and bh.replace(".", "").isdigit():
                fw, fh = float(bw), float(bh)
                if fw > 0 and fh > 0:
                    return fw, fh
    return 640.0, 360.0


def _translate_delta(transform: str) -> tuple[float, float]:
    dx = 0.0
    dy = 0.0
    for m in _TRANSLATE_RE.finditer(transform or ""):
        dx += float(m.group(1))
        dy += float(m.group(2)) if m.group(2) is not None else 0.0
    return dx, dy


def _plain_text(inner: str) -> str:
    stripped = _TAG_RE.sub("", inner or "")
    return unescape(stripped).replace("\n", " ").strip()


def _esc_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_TAG_EVENT_RE = re.compile(r"<(/?)(g|svg|text)\b([^>]*)>", re.IGNORECASE)


def _ancestor_translates(svg: str, text_starts: list[int]) -> list[tuple[float, float]]:
    """每个 ``<text>`` 起点处，祖先 ``g``/``svg`` 的累计 translate。"""
    wanted = set(text_starts)
    found: dict[int, tuple[float, float]] = {}
    stack_x = [0.0]
    stack_y = [0.0]
    for m in _TAG_EVENT_RE.finditer(svg):
        closing = bool(m.group(1))
        name = m.group(2).lower()
        if name == "text" and not closing and m.start() in wanted:
            found[m.start()] = (stack_x[-1], stack_y[-1])
            continue
        if name not in ("g", "svg"):
            continue
        if closing:
            if len(stack_x) > 1:
                stack_x.pop()
                stack_y.pop()
            continue
        ad = _parse_attrs(m.group(3) or "")
        tdx, tdy = _translate_delta(ad["transform"] if "transform" in ad else "")
        stack_x.append(stack_x[-1] + tdx)
        stack_y.append(stack_y[-1] + tdy)
    return [found[p] if p in found else (0.0, 0.0) for p in text_starts]


def _span_from_text(inner: str, attrs: dict[str, str], extra_xy: tuple[float, float] = (0.0, 0.0)) -> str:
    body = _plain_text(inner)
    if not body:
        return ""
    x = _num_attr(attrs, "x", 0.0) + extra_xy[0]
    y = _num_attr(attrs, "y", 0.0) + extra_xy[1]
    fs = _num_attr(attrs, "font-size", 12.0)
    if fs <= 0:
        fs = 12.0
    fill = attrs["fill"] if "fill" in attrs and attrs["fill"] else "#e2e8f0"
    weight = attrs["font-weight"] if "font-weight" in attrs else ""
    dx, dy = _translate_delta(attrs["transform"] if "transform" in attrs else "")
    x += dx
    y += dy
    # SVG y 是基线；HTML top 是盒顶，上移约 0.8em
    top = y - fs * 0.82
    anchor = (attrs["text-anchor"] if "text-anchor" in attrs else "start").strip().lower()
    if anchor == "middle":
        tx = "translate(-50%,0)"
    elif anchor == "end":
        tx = "translate(-100%,0)"
    else:
        tx = "none"
    weight_css = f"font-weight:{weight};" if weight else ""
    style = f"left:{x:.1f}px;top:{top:.1f}px;font-size:{fs:.1f}px;color:{fill};transform:{tx};{weight_css}"
    return f'<span class="svg-label" style="{style}">{_esc_html(body)}</span>'


def _rewrite_one_svg(svg: str) -> str:
    texts = list(_SVG_TEXT_RE.finditer(svg))
    if not texts:
        return svg
    offsets = _ancestor_translates(svg, [t.start() for t in texts])
    spans: list[str] = []
    for t, extra in zip(texts, offsets):
        spans.append(_span_from_text(t.group(2), _parse_attrs(t.group(1)), extra))
    shapes = _SVG_TEXT_RE.sub("", svg)
    w, h = _svg_size(svg)
    overlay = "".join(s for s in spans if s)
    return f'<div class="svg-wrap" style="position:relative;width:{w:.0f}px;height:{h:.0f}px">{shapes}{overlay}</div>'


def rewrite_svg_charts_for_takumi(html: str) -> str:
    """内联 SVG 的 ``<text>`` 提升为 HTML 覆盖层；形状保留。幂等。"""
    if not html or "<svg" not in html.lower():
        return html
    if "<text" not in html.lower():
        return html
    out = _SVG_BLOCK_RE.sub(lambda m: _rewrite_one_svg(m.group(0)), html)
    if "svg-wrap" in out and ".svg-wrap{" not in out.replace(" ", ""):
        out = f"<style>{_OVERLAY_CSS}</style>\n{out}"
    return out
