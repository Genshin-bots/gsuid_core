"""把 HTML / Markdown 里的 Mermaid 换成 mermaid.ink 的 SVG，再交给 pytakumi。

Takumi 不跑页面脚本，也不吃 SVG 样式表里的 class 上色，foreignObject 里的字会丢。
这里把颜色写进属性，把标签改成带 MiSans 的 ``<text>``，按 viewBox 1:1 摆放。
mermaid.ink 连不上时只换掉这一块，其余 HTML 照常出图。
"""

from __future__ import annotations

import re
import base64
from html import escape, unescape
from dataclasses import dataclass

import httpx
import lxml.etree as etree

from gsuid_core.i18n import t
from gsuid_core.logger import logger

_SVG_NS = "http://www.w3.org/2000/svg"
_INK_SVG = "https://mermaid.ink/svg/"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_HEADERS = {"User-Agent": "gsuid-core"}
_MAX_SVG_CHARS = 2_000_000
_SHAPE_TAGS = frozenset({"rect", "circle", "ellipse", "polygon", "polyline", "path", "line"})
_PAINT = frozenset({"fill", "stroke", "stroke-width", "stroke-dasharray", "opacity"})
_MERMAID_CLASS = frozenset({"mermaid", "language-mermaid", "lang-mermaid"})
_LABEL_PX = 16.0

_OPEN_RE = re.compile(
    r"<(?P<tag>pre|div|span|code)\b(?P<attrs>[^>]*)>",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"```mermaid[ \t]*\r?\n(.*?)```", re.IGNORECASE | re.DOTALL)
_CLASS_RE = re.compile(r"""class\s*=\s*(?P<q>['"])(?P<cls>.*?)(?P=q)""", re.IGNORECASE | re.DOTALL)
_CLASS_BARE_RE = re.compile(r"""class\s*=\s*([^\s"'=<>`]+)""", re.IGNORECASE)
_CODE_WRAP_RE = re.compile(r"<code\b[^>]*>(.*)</code>", re.IGNORECASE | re.DOTALL)
_PRE_BEFORE_RE = re.compile(r"<pre\b[^>]*>\s*$", re.IGNORECASE)
_PRE_AFTER_RE = re.compile(r"\s*</pre\s*>", re.IGNORECASE)
_RULE_RE = re.compile(r"([^{}@]+)\{([^{}]*)\}")
_IMPORT_RE = re.compile(r"@import[^;]*;", re.IGNORECASE)
_KEYFRAMES_RE = re.compile(r"@keyframes\s+[^{]+\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", re.IGNORECASE)
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_TF_RE = re.compile(r"(matrix|translate|scale)\(([^)]*)\)")
_NUM_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_FILL_IN_STYLE_RE = re.compile(r"(?:^|;)\s*fill\s*:", re.IGNORECASE)

_IDENT = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_Matrix = tuple[float, float, float, float, float, float]


class MermaidInkError(Exception):
    """mermaid.ink 没有交回可绘制的 SVG。"""

    def __init__(self, message: str, *, unavailable: bool) -> None:
        super().__init__(message)
        self.unavailable = unavailable


@dataclass(frozen=True)
class _Block:
    start: int
    end: int
    source: str


@dataclass(frozen=True)
class _Sel:
    tag: str | None
    ident: str | None
    classes: tuple[str, ...]

    @property
    def spec(self) -> int:
        score = 0
        if self.tag:
            score += 1
        if self.ident:
            score += 100
        score += 10 * len(self.classes)
        return score


def fetch_mermaid_svg(code: str, *, client: httpx.Client | None = None) -> str:
    """按 mermaid.ink 的 base64 URL 取 SVG。失败抛 :class:`MermaidInkError`。"""
    token = base64.urlsafe_b64encode(code.encode("utf-8")).decode("ascii")
    url = _INK_SVG + token
    if client is None:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as owned:
            response = owned.get(url)
    else:
        response = client.get(url)
    return _svg_or_error(response)


def expand_mermaid_in_html(html: str) -> str:
    """替换 Mermaid 块。没有 Mermaid 时原样返回。单块失败不影响其他块。"""
    blocks = _find_blocks(html)
    if not blocks:
        return html
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as client:
        out = html
        for block in reversed(blocks):
            out = out[: block.start] + _replacement(block.source, client) + out[block.end :]
        return out


def adapt_mermaid_svg(svg: str) -> str:
    """把 mermaid.ink 的 SVG 收成 pytakumi 能画的 1:1 图。"""
    parser = etree.XMLParser(recover=True, huge_tree=True)
    try:
        root = etree.fromstring(svg.encode("utf-8"), parser)
    except etree.XMLSyntaxError as exc:
        raise MermaidInkError("svg parse", unavailable=True) from exc
    if not isinstance(root.tag, str) or _local(root.tag) != "svg":
        raise MermaidInkError("not svg", unavailable=True)

    sheets = _take_styles(root)
    _apply_paint(root, _paint_rules(sheets))
    _lift_foreign_labels(root)
    _force_label_font(root)
    if not _has_paint(root):
        raise MermaidInkError("empty svg", unavailable=True)

    min_x, min_y, box_w, box_h = _view_box(root)
    background = etree.Element(f"{{{_SVG_NS}}}rect")
    background.set("x", _num(min_x))
    background.set("y", _num(min_y))
    background.set("width", _num(box_w))
    background.set("height", _num(box_h))
    background.set("fill", "#ffffff")
    root.insert(0, background)

    if "style" in root.attrib:
        del root.attrib["style"]
    root.set("data-gs-mermaid", "1")
    root.set("width", _num(box_w))
    root.set("height", _num(box_h))
    root.set("style", f"width:{box_w:.1f}px;height:{box_h:.1f}px;")
    xml = etree.tostring(root, encoding="unicode")
    return f'<div style="margin:8px 0;line-height:0;">{xml}</div>'


def _svg_or_error(response: httpx.Response) -> str:
    status = response.status_code
    text = response.text
    if status != 200:
        unavailable = status >= 500 or status in {408, 429}
        raise MermaidInkError(f"HTTP {status}", unavailable=unavailable)
    if len(text) > _MAX_SVG_CHARS or "<svg" not in text[:4000].lower():
        raise MermaidInkError("not svg", unavailable=True)
    return text


def _replacement(source: str, client: httpx.Client) -> str:
    if not source.strip():
        return ""
    try:
        return adapt_mermaid_svg(fetch_mermaid_svg(source, client=client))
    except Exception as exc:
        # 外部 SVG / mermaid.ink 失败只换这一块，不能让整页出图失败
        unavailable = exc.unavailable if isinstance(exc, MermaidInkError) else True
        detail = str(exc).replace("{", "(").replace("}", ")")
        logger.warning(t("log.htmlrender.mermaid_unavailable", e=detail))
        if unavailable:
            return _terminal("服务不可用", "mermaid.ink 没有响应")
        return _terminal("这张图无法渲染", "mermaid.ink 已响应，但没有图")


def _terminal(primary: str, secondary: str) -> str:
    title = escape("mermaid.ink")
    head = escape(primary)
    sub = escape(secondary)
    return (
        '<div style="margin:12px 0;max-width:100%;background:#0c0c0c;border:1px solid #333;'
        'border-radius:8px;overflow:hidden;">'
        '<div style="background:#1a1a1a;color:#9aa0a6;font-family:Mono,Consolas,monospace;'
        f'font-size:13px;padding:8px 14px;">{title}</div>'
        '<div style="padding:14px 16px 16px;">'
        '<div style="color:#ff2a2a;font-family:MiSans,sans-serif;font-size:20px;'
        f'font-weight:630;">{head}</div>'
        '<div style="color:#ff5c5c;font-family:MiSans,sans-serif;font-size:14px;'
        f'margin-top:8px;">{sub}</div>'
        "</div></div>"
    )


def _find_blocks(html: str) -> list[_Block]:
    found: list[_Block] = []
    # 只从带 mermaid class 的标签起找，避免外层 pre 把里面的 code 整段吃掉
    for match in _OPEN_RE.finditer(html):
        tokens = _class_tokens(match.group("attrs"))
        if tokens.isdisjoint(_MERMAID_CLASS):
            continue
        close = _matching_close(html, match.end(), match.group("tag"))
        if close is None:
            continue
        body = html[match.end() : close[0]]
        if "<svg" in body.lower():
            continue
        start, end = _absorb_pre(html, match.start(), close[1], match.group("tag"))
        found.append(_Block(start, end, _diagram_source(body)))
    for match in _FENCE_RE.finditer(html):
        found.append(_Block(match.start(), match.end(), unescape(match.group(1)).strip()))
    return _dedupe(found)


def _matching_close(html: str, content_start: int, tag: str) -> tuple[int, int] | None:
    depth = 1
    token = re.compile(rf"<(/?){re.escape(tag)}\b[^>]*>", re.IGNORECASE)
    for match in token.finditer(html, content_start):
        if match.group(1):
            depth -= 1
            if depth == 0:
                return match.start(), match.end()
            continue
        depth += 1
    return None


def _class_tokens(attrs: str) -> set[str]:
    quoted = _CLASS_RE.search(attrs)
    raw = quoted.group("cls") if quoted is not None else ""
    if not raw:
        bare = _CLASS_BARE_RE.search(attrs)
        raw = bare.group(1) if bare is not None else ""
    return {part.lower() for part in raw.split()}


def _absorb_pre(html: str, start: int, end: int, tag: str) -> tuple[int, int]:
    if tag.lower() != "code":
        return start, end
    pre = _PRE_BEFORE_RE.search(html[:start])
    close = _PRE_AFTER_RE.match(html[end:])
    if pre is None or close is None:
        return start, end
    return pre.start(), end + close.end()


def _diagram_source(body: str) -> str:
    text = unescape(body).strip()
    wrapped = _CODE_WRAP_RE.fullmatch(text)
    if wrapped is not None:
        text = unescape(wrapped.group(1)).strip()
    return text


def _dedupe(blocks: list[_Block]) -> list[_Block]:
    ordered = sorted(blocks, key=lambda block: (block.start, -block.end))
    kept: list[_Block] = []
    for block in ordered:
        if kept and kept[-1].start <= block.start and block.end <= kept[-1].end:
            continue
        kept.append(block)
    return kept


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower() if tag.startswith("{") else tag.lower()


def _take_styles(root: etree._Element) -> list[str]:
    sheets: list[str] = []
    for el in list(root.iter()):
        if not isinstance(el.tag, str) or _local(el.tag) != "style":
            continue
        sheets.append(el.text or "")
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
    return sheets


def _paint_rules(sheets: list[str]) -> list[tuple[int, int, list[_Sel], dict[str, str]]]:
    rules: list[tuple[int, int, list[_Sel], dict[str, str]]] = []
    order = 0
    for sheet in sheets:
        cleaned = _COMMENT_RE.sub("", sheet)
        cleaned = _IMPORT_RE.sub("", cleaned)
        cleaned = _KEYFRAMES_RE.sub("", cleaned)
        for sel_raw, block in _RULE_RE.findall(cleaned):
            decls = _paint_decls(block)
            if not decls:
                continue
            for sel in sel_raw.split(","):
                parts = _selector(sel)
                if not parts:
                    continue
                spec = sum(part.spec for part in parts)
                rules.append((spec, order, parts, decls))
                order += 1
    return rules


def _paint_decls(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for piece in block.split(";"):
        if ":" not in piece:
            continue
        key_raw, val_raw = piece.split(":", 1)
        key = key_raw.strip().lower()
        val = val_raw.replace("!important", "").strip()
        if key not in _PAINT or not val:
            continue
        if key == "stroke-width" and val.endswith("px"):
            val = val[:-2].strip()
        out[key] = val
    return out


def _selector(raw: str) -> list[_Sel]:
    body = raw.strip()
    if not body or ":" in body or "[" in body:
        return []
    parts: list[_Sel] = []
    for chunk in body.replace(">", " ").split():
        part = _parse_part(chunk)
        if part is None:
            return []
        parts.append(part)
    return parts


def _parse_part(raw: str) -> _Sel | None:
    if not raw or raw == "*":
        return None
    tag: str | None = None
    ident: str | None = None
    classes: list[str] = []
    buf = ""
    index = 0
    while index < len(raw):
        ch = raw[index]
        if ch in ".#":
            if buf and tag is None:
                tag = buf.lower()
            buf = ""
            index += 1
            start = index
            while index < len(raw) and raw[index] not in ".#":
                index += 1
            token = raw[start:index]
            if not token:
                continue
            if ch == "#":
                ident = token
            else:
                classes.append(token)
            continue
        buf += ch
        index += 1
    if buf and tag is None:
        tag = buf.lower()
    if tag is None and ident is None and not classes:
        return None
    return _Sel(tag, ident, tuple(classes))


def _match_part(el: etree._Element, part: _Sel) -> bool:
    if not isinstance(el.tag, str):
        return False
    if part.tag is not None and _local(el.tag) != part.tag:
        return False
    if part.ident is not None:
        current = el.attrib["id"] if "id" in el.attrib else ""
        if current != part.ident:
            return False
    if part.classes:
        have = set((el.attrib["class"] if "class" in el.attrib else "").split())
        if not set(part.classes).issubset(have):
            return False
    if part.tag is None and part.ident is None and not part.classes:
        return False
    return True


def _matches(el: etree._Element, parts: list[_Sel]) -> bool:
    if not parts or not _match_part(el, parts[-1]):
        return False
    parent = el.getparent()
    for part in reversed(parts[:-1]):
        found = False
        while parent is not None:
            if _match_part(parent, part):
                found = True
                parent = parent.getparent()
                break
            parent = parent.getparent()
        if not found:
            return False
    return True


def _apply_paint(root: etree._Element, rules: list[tuple[int, int, list[_Sel], dict[str, str]]]) -> None:
    applied: dict[int, dict[str, tuple[int, int]]] = {}
    for el in root.iter():
        if not isinstance(el.tag, str) or _local(el.tag) not in _SHAPE_TAGS:
            continue
        inline = _paint_decls(el.attrib["style"] if "style" in el.attrib else "")
        slot = applied.setdefault(id(el), {})
        for spec, order, parts, decls in rules:
            if not _matches(el, parts):
                continue
            for key, val in decls.items():
                if key in inline:
                    continue
                prev = slot[key] if key in slot else None
                if prev is not None and (spec, order) < prev:
                    continue
                slot[key] = (spec, order)
                el.set(key, val)


def _lift_foreign_labels(root: etree._Element) -> None:
    def walk(el: etree._Element, mat: _Matrix) -> None:
        here = _mul(mat, _parse_transform(el.attrib["transform"] if "transform" in el.attrib else ""))
        if isinstance(el.tag, str) and _local(el.tag) == "foreignobject":
            label = _foreign_label(el)
            parent = el.getparent()
            if label and parent is not None:
                x = _px(el.attrib["x"] if "x" in el.attrib else None)
                y = _px(el.attrib["y"] if "y" in el.attrib else None)
                w = _px(el.attrib["width"] if "width" in el.attrib else None)
                h = _px(el.attrib["height"] if "height" in el.attrib else None)
                cx, cy = _apply(here, x + w / 2.0, y + h / 2.0)
                root.append(_label_text(cx, cy, label))
            if parent is not None:
                parent.remove(el)
            return
        for child in list(el):
            walk(child, here)

    walk(root, _IDENT)


def _foreign_label(el: etree._Element) -> str:
    lines: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        piece = re.sub(r"\s+", " ", "".join(buf)).strip()
        buf.clear()
        if piece:
            lines.append(piece)

    def walk(node: etree._Element) -> None:
        if node.text:
            buf.append(node.text)
        for child in node:
            if not isinstance(child.tag, str):
                continue
            name = _local(child.tag)
            if name == "br":
                flush()
            elif name == "p":
                flush()
                walk(child)
                flush()
            else:
                walk(child)
            if child.tail:
                buf.append(child.tail)

    walk(el)
    flush()
    return "\n".join(lines)


def _label_text(x: float, y: float, label: str) -> etree._Element:
    text = etree.Element(f"{{{_SVG_NS}}}text")
    text.set("x", _num(x))
    text.set("y", _num(y))
    text.set("fill", "#333333")
    text.set("text-anchor", "middle")
    text.set("dominant-baseline", "central")
    text.set("font-family", "MiSans")
    text.set("font-size", _num(_LABEL_PX))
    lines = label.split("\n")
    if len(lines) == 1:
        text.text = lines[0]
        return text
    first_y = y - (len(lines) - 1) * _LABEL_PX * 0.5
    text.set("y", _num(first_y))
    text.text = lines[0]
    for line in lines[1:]:
        span = etree.SubElement(text, f"{{{_SVG_NS}}}tspan")
        span.set("x", _num(x))
        span.set("dy", _num(_LABEL_PX))
        span.set("font-family", "MiSans")
        span.text = line
    return text


def _force_label_font(root: etree._Element) -> None:
    for el in root.iter():
        if not isinstance(el.tag, str) or _local(el.tag) not in {"text", "tspan"}:
            continue
        el.set("font-family", "MiSans")
        style = el.attrib["style"] if "style" in el.attrib else ""
        if "font-family" in style.lower():
            style = re.sub(r"font-family\s*:[^;]*", "font-family:MiSans", style, flags=re.IGNORECASE)
        elif style:
            style = style.rstrip(";") + ";font-family:MiSans"
        else:
            style = "font-family:MiSans"
        el.set("style", style)
        if "fill" not in el.attrib and _FILL_IN_STYLE_RE.search(style) is None:
            el.set("fill", "#333333")


def _has_paint(root: etree._Element) -> bool:
    for el in root.iter():
        if isinstance(el.tag, str) and _local(el.tag) in _SHAPE_TAGS.union({"text"}):
            return True
    return False


def _view_box(root: etree._Element) -> tuple[float, float, float, float]:
    raw = root.attrib["viewBox"] if "viewBox" in root.attrib else ""
    nums = [_px(part) for part in raw.replace(",", " ").split()[:4]]
    if len(nums) == 4 and nums[2] > 0 and nums[3] > 0:
        return nums[0], nums[1], nums[2], nums[3]
    width = _px(root.attrib["width"] if "width" in root.attrib else None)
    height = _px(root.attrib["height"] if "height" in root.attrib else None)
    if width > 0 and height > 0:
        return 0.0, 0.0, width, height
    return 0.0, 0.0, 400.0, 200.0


def _px(raw: str | None) -> float:
    if not raw:
        return 0.0
    body = raw.strip().replace("px", "")
    if not body or body.endswith("%"):
        return 0.0
    sign = 1.0
    if body[0] in "+-":
        sign = -1.0 if body[0] == "-" else 1.0
        body = body[1:]
    if not body or body.count(".") > 1 or not body.replace(".", "").isdigit():
        return 0.0
    return sign * float(body)


def _num(value: float) -> str:
    return f"{value:.2f}"


def _parse_transform(raw: str) -> _Matrix:
    acc = _IDENT
    if not raw:
        return acc
    for kind, args in _TF_RE.findall(raw):
        nums = [float(item) for item in _NUM_RE.findall(args)]
        if kind == "translate" and nums:
            mat = (1.0, 0.0, 0.0, 1.0, nums[0], nums[1] if len(nums) > 1 else 0.0)
        elif kind == "scale" and nums:
            sx = nums[0]
            sy = nums[1] if len(nums) > 1 else sx
            mat = (sx, 0.0, 0.0, sy, 0.0, 0.0)
        elif kind == "matrix" and len(nums) >= 6:
            mat = (nums[0], nums[1], nums[2], nums[3], nums[4], nums[5])
        else:
            continue
        acc = _mul(acc, mat)
    return acc


def _mul(left: _Matrix, right: _Matrix) -> _Matrix:
    a1, b1, c1, d1, e1, f1 = left
    a2, b2, c2, d2, e2, f2 = right
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _apply(mat: _Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = mat
    return a * x + c * y + e, b * x + d * y + f
