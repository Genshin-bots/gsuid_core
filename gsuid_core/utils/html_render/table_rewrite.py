"""将 HTML ``<table>`` 改写为 flex 行网格，供 pytakumi/Takumi 绘制。

Takumi 布局引擎不实现 CSS table 模型；旧版 pytakumi 的 GFM 表会走
``table { display:block }`` 并塌成一行字。新版 pytakumi 提供
``rewrite_tables_for_takumi``；本模块：

1. 优先委托新版 pytakumi（markdown 路径）；
2. 自由 HTML 路径使用内置实现，保留 ``td/th`` 的 class/style。

**范围**：仅简单行/单元格表（``tr`` / ``th`` / ``td``）。嵌套 ``<table>``、
``colspan`` / ``rowspan`` 不保证语义完整——LLM 出图请展平为单层表。
"""

from __future__ import annotations

import re

_ALIGN_BY_MARKER: dict[str, str] = {
    "left": "md-table-cell-left",
    "right": "md-table-cell-right",
    "center": "md-table-cell-center",
}

# 只搬「标识符」class，避免把布局 class 整坨拷坏
_CLASS_TOKEN_RE = re.compile(r"[A-Za-z_][\w:-]*")


def _cell_align_from_attrs(cell_open_tag: str) -> str | None:
    style = re.search(
        r'style\s*=\s*["\'][^"\']*text-align\s*:\s*(left|right|center)',
        cell_open_tag,
        flags=re.I,
    )
    if style:
        return style.group(1).lower()
    return None


def _classes_from_open_tag(cell_open_tag: str) -> list[str]:
    """从 ``<td class="up big">`` 抽出 class token。"""
    m = re.search(r'class\s*=\s*["\']([^"\']*)["\']', cell_open_tag, flags=re.I)
    if not m:
        return []
    return [t for t in _CLASS_TOKEN_RE.findall(m.group(1)) if t]


def _inline_style_from_open_tag(cell_open_tag: str) -> str:
    """保留 text-align 以外的 inline style（颜色/字重等）。"""
    m = re.search(r'style\s*=\s*["\']([^"\']*)["\']', cell_open_tag, flags=re.I)
    if not m:
        return ""
    parts: list[str] = []
    for decl in m.group(1).split(";"):
        d = decl.strip()
        if not d:
            continue
        if re.match(r"text-align\s*:", d, flags=re.I):
            continue
        parts.append(d)
    return "; ".join(parts)


def _column_alignments(rows: list[tuple[list[tuple[str, str]], bool]]) -> list[str | None]:
    n_cols = max((len(cells) for cells, _ in rows), default=0)
    aligns: list[str | None] = [None] * n_cols
    ordered = [cells for cells, is_header in rows if is_header]
    ordered.extend(cells for cells, is_header in rows if not is_header)
    for cells in ordered:
        changed = False
        for idx, (open_tag, _cell_html) in enumerate(cells):
            if idx >= n_cols or aligns[idx] is not None:
                continue
            align = _cell_align_from_attrs(open_tag)
            if align is not None:
                aligns[idx] = align
                changed = True
        if changed and all(a is not None for a in aligns):
            break
    return aligns


def _cell_is_numeric(cell_html: str) -> bool:
    text = re.sub(r"<[^>]+>", "", cell_html)
    text = re.sub(r"&[a-zA-Z]+;|&#\d+;", "", text).strip()
    if not text:
        return False
    return bool(
        re.fullmatch(r"[+\-]?[\d,]*\.?\d+(?:[eE][+\-]?\d+)?[%‰]?", text)
        or re.fullmatch(r"[+\-]?\(?\d[\d,]*\.?\d*\)?", text)
    )


def _rewrite_tables_local(html: str) -> str:
    """内置改写：``<table>`` → ``.md-table`` flex 行（非嵌套简单表）。"""
    if "<table" not in html.lower():
        return html

    def _cell_pairs(row_inner: str) -> list[tuple[str, str]]:
        matches: list[tuple[str, str]] = re.findall(r"(<t[hd][^>]*>)(.*?)</t[hd]>", row_inner, flags=re.I | re.S)
        return [(open_tag, inner) for open_tag, inner in matches]

    def _repl_table(match: re.Match[str]) -> str:
        table = match.group(0)
        # 嵌套表：内层 table 会使非贪婪匹配提前结束 → 原样保留外层
        if table.lower().count("<table") > 1:
            return table
        rows: list[str] = re.findall(r"<tr[^>]*>(.*?)</tr>", table, flags=re.I | re.S)
        if not rows:
            return table

        parsed: list[tuple[list[tuple[str, str]], bool]] = []
        for row in rows:
            cells = _cell_pairs(row)
            if not cells:
                continue
            is_header = bool(re.search(r"<th\b", row, flags=re.I))
            parsed.append((cells, is_header))
        if not parsed:
            return table

        n_cols = max(len(c) for c, _ in parsed)
        if n_cols == 0:
            return table

        aligns = _column_alignments(parsed)
        while len(aligns) < n_cols:
            aligns.append(None)

        parts = ['<div class="md-table" role="table">']
        for r_i, (cells, is_header) in enumerate(parsed):
            while len(cells) < n_cols:
                cells.append(("<td>", ""))
            row_cls = "md-table-row md-table-header" if is_header else "md-table-row"
            if not is_header and r_i % 2 == 0:
                row_cls += " md-table-row-alt"
            parts.append(f'<div class="{row_cls}" role="row">')
            for c_i, (open_tag, cell) in enumerate(cells):
                cell_cls = "md-table-cell md-table-th" if is_header else "md-table-cell"
                # 保留原 td/th class（涨跌色 .up/.down 等）
                for extra_cls in _classes_from_open_tag(open_tag):
                    if extra_cls not in cell_cls.split():
                        cell_cls += f" {extra_cls}"
                align = aligns[c_i]
                if align is None:
                    align = _cell_align_from_attrs(open_tag)
                if align is None and not is_header and _cell_is_numeric(cell):
                    align = "right"
                if align is not None:
                    marker = _ALIGN_BY_MARKER[align]
                    if marker not in cell_cls.split():
                        cell_cls += f" {marker}"
                if c_i < n_cols - 1:
                    cell_cls += " md-table-cell-border"
                style_attr = _inline_style_from_open_tag(open_tag)
                style_html = f' style="{style_attr}"' if style_attr else ""
                parts.append(f'<div class="{cell_cls}" role="cell"{style_html}>{cell}</div>')
            parts.append("</div>")
        parts.append("</div>")
        return "".join(parts)

    return re.sub(r"<table\b[^>]*>.*?</table>", _repl_table, html, flags=re.I | re.S)


def rewrite_tables_for_takumi(html: str, *, prefer_local: bool = False) -> str:
    """Rewrite ``<table>…</table>`` into flex-row markup Takumi can paint.

    Args:
        prefer_local: True 时强制用内置实现（自由 HTML：保留 class/style）。
    """
    if not html or "<table" not in html.lower():
        return html
    if prefer_local:
        return _rewrite_tables_local(html)
    # 新版 pytakumi 提供同名 API；缺省或旧版无该符号时用内置实现
    try:
        from pytakumi.markdown import rewrite_tables_for_takumi as _upstream
    except ImportError:
        return _rewrite_tables_local(html)
    if not callable(_upstream):
        return _rewrite_tables_local(html)
    return _upstream(html)


# 布局 only：不设文字色；斑马用半透明灰，深浅页面都可读；不盖 .up/.down
MD_TABLE_LAYOUT_CSS = """
.md-table{
  display:flex;flex-direction:column;width:100%;margin:12px 0;
  border:1px solid rgba(128,128,128,0.35);border-radius:8px;overflow:hidden;
}
.md-table-row{
  display:flex;flex-direction:row;width:100%;
  border-top:1px solid rgba(128,128,128,0.28);align-items:stretch;
}
.md-table-row:first-child{border-top:none;}
.md-table-header{font-weight:700;background:rgba(128,128,128,0.18);}
.md-table-row-alt{background:rgba(128,128,128,0.08);}
.md-table-cell{flex:1 1 0;padding:8px 12px;min-width:0;font-size:13px;line-height:1.4;}
.md-table-th{font-weight:700;}
.md-table-cell-border{border-right:1px solid rgba(128,128,128,0.28);}
.md-table-cell-left{text-align:left;}
.md-table-cell-right{text-align:right;}
.md-table-cell-center{text-align:center;}
""".strip()

# 浅色主题（markdown 亮色 / 显式 light）
MD_TABLE_FLEX_CSS_LIGHT = """
.md-table{
  display:flex;flex-direction:column;width:100%;margin:12px 0;
  border:1px solid #d1d9e0;border-radius:8px;overflow:hidden;
}
.md-table-row{
  display:flex;flex-direction:row;width:100%;
  border-top:1px solid #d1d9e0;align-items:stretch;background:#fff;
}
.md-table-row:first-child{border-top:none;}
.md-table-header{font-weight:700;background:#eef3f9;color:#1f2937;}
.md-table-row-alt{background:#f7f9fc;}
.md-table-cell{flex:1 1 0;padding:8px 12px;min-width:0;font-size:13px;line-height:1.4;color:#1f2937;}
.md-table-th{font-weight:700;color:#111827;}
.md-table-cell-border{border-right:1px solid #d1d9e0;}
.md-table-cell-left{text-align:left;}
.md-table-cell-right{text-align:right;}
.md-table-cell-center{text-align:center;}
""".strip()

# 深色主题（自由 HTML 深色 body / markdown dark）
MD_TABLE_FLEX_CSS_DARK = """
.md-table{
  display:flex;flex-direction:column;width:100%;margin:12px 0;
  border:1px solid #2f3d56;border-radius:8px;overflow:hidden;
}
.md-table-row{
  display:flex;flex-direction:row;width:100%;
  border-top:1px solid #2f3d56;align-items:stretch;background:#1a2538;
}
.md-table-row:first-child{border-top:none;}
.md-table-header{font-weight:700;background:#5b9dd9;color:#061018;}
.md-table-row-alt{background:#202c42;}
.md-table-cell{flex:1 1 0;padding:8px 12px;min-width:0;font-size:13px;line-height:1.4;color:#f2f6fc;}
.md-table-th{font-weight:700;color:#061018;}
.md-table-cell-border{border-right:1px solid #2f3d56;}
.md-table-header .md-table-cell-border{border-right-color:rgba(6,16,24,0.2);}
.md-table-cell-left{text-align:left;}
.md-table-cell-right{text-align:right;}
.md-table-cell-center{text-align:center;}
""".strip()

# 兼容旧名：默认 layout-only（自由 HTML 注入用，不覆盖 agent 配色）
MD_TABLE_FLEX_CSS = MD_TABLE_LAYOUT_CSS


def md_table_flex_css(
    *,
    markdown_body: bool = False,
    theme: str = "layout",
) -> str:
    """``.md-table`` 样式。

    theme:
      - ``layout``：无硬编码色（自由 HTML 默认，避免深色页浅色表）
      - ``light`` / ``dark``：完整主题色
    """
    if theme == "dark":
        css = MD_TABLE_FLEX_CSS_DARK
    elif theme == "light":
        css = MD_TABLE_FLEX_CSS_LIGHT
    else:
        css = MD_TABLE_LAYOUT_CSS
    if not markdown_body:
        return css
    return css.replace(".md-table", ".markdown-body .md-table")


def detect_html_table_theme(html: str) -> str:
    """粗判页面偏深/偏浅，供注入完整主题时选用；自由 HTML 优先 layout。"""
    sample = html[:4000].lower()
    # 常见深色背景 token
    dark_hits = sum(
        1
        for t in (
            "background:#0",
            "background: #0",
            "background:#1",
            "background: #1",
            "background-color:#0",
            "background-color:#1",
            "#0f172a",
            "#0d1424",
            "#080d18",
            "#111827",
            "#0a1020",
            "color:#e2e8f0",
            "color: #e2e8f0",
            "color:#edf2fa",
            "color:#f2f6fc",
        )
        if t in sample
    )
    light_hits = sum(
        1
        for t in (
            "background:#fff",
            "background: #fff",
            "background:#f",
            "background-color:#fff",
            "background:white",
            "color:#111",
            "color:#333",
            "color:#1f2937",
        )
        if t in sample
    )
    if dark_hits > light_hits and dark_hits >= 1:
        return "dark"
    if light_hits > dark_hits and light_hits >= 1:
        return "light"
    # 无信号：layout-only，不覆盖 body 继承色
    return "layout"
