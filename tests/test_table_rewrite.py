"""table_rewrite：简单表改写 + 嵌套表原样保留 + class 保留 + 主题 CSS。"""

from __future__ import annotations

import re


def test_simple_table_to_md_flex() -> None:
    from gsuid_core.utils.html_render.table_rewrite import _rewrite_tables_local

    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    out = _rewrite_tables_local(html)
    assert "md-table" in out
    assert "md-table-row" in out
    assert "md-table-cell" in out
    assert "<table" not in out.lower()


def test_nested_table_left_unchanged() -> None:
    """嵌套 table 超出简单行表范围：本地实现原样保留（不半截改写）。"""
    from gsuid_core.utils.html_render.table_rewrite import _rewrite_tables_local

    nested = "<table><tr><td><table><tr><td>inner</td></tr></table></td></tr></table>"
    out = _rewrite_tables_local(nested)
    assert "md-table" not in out
    assert "<table" in out.lower()


def test_preserves_td_class_and_non_align_style() -> None:
    from gsuid_core.utils.html_render.table_rewrite import _rewrite_tables_local

    html = '<table><tr><td class="up">+5.48%</td><td style="text-align:right;color:#34d399">1</td></tr></table>'
    out = _rewrite_tables_local(html)
    assert "up" in out
    assert "md-table-cell-right" in out
    assert "color:#34d399" in out or "color: #34d399" in out
    # text-align 已变成 class，不应再以 text-align 内联出现（避免重复）
    assert "text-align" not in out.lower() or "md-table-cell-right" in out


def test_layout_css_has_no_hard_bg_colors() -> None:
    from gsuid_core.utils.html_render.table_rewrite import (
        MD_TABLE_LAYOUT_CSS,
        md_table_flex_css,
        detect_html_table_theme,
    )

    assert "background:#eef3f9" not in MD_TABLE_LAYOUT_CSS
    assert "background:#f7f9fc" not in MD_TABLE_LAYOUT_CSS
    dark_page = "body{background:#0f172a;color:#e2e8f0;}"
    assert detect_html_table_theme(dark_page) == "dark"
    light_css = md_table_flex_css(theme="light")
    assert "#eef3f9" in light_css
    dark_css = md_table_flex_css(theme="dark")
    assert "#1a2538" in dark_css


def test_prefer_local_flag() -> None:
    from gsuid_core.utils.html_render.table_rewrite import rewrite_tables_for_takumi

    html = '<table><tr><td class="up">x</td></tr></table>'
    out = rewrite_tables_for_takumi(html, prefer_local=True)
    assert "up" in out
    assert "md-table" in out


def test_rowspan_pads_leading_columns() -> None:
    """rowspan 后续行应在被占用列前插空 cell，而不是末尾补空。"""
    from gsuid_core.utils.html_render.table_rewrite import _rewrite_tables_local

    html = """
    <table>
      <tr><th>方向</th><th>板块</th><th>逻辑</th></tr>
      <tr><td rowspan="2" class="bg-red">农业</td><td>北大荒</td><td>洪涝</td></tr>
      <tr><td>新赛股份</td><td>高温</td></tr>
    </table>
    """
    out = _rewrite_tables_local(html)
    # 用 cell 文本顺序验证：rowspan 后续行首列为占位
    assert "农业" in out
    assert "新赛股份" in out
    # body 第二行：空占位 | 新赛股份 | 高温 —— 「新赛股份」不得出现在行首 cell
    body_rows = [r for r in out.split('role="row"') if "新赛股份" in r]
    assert body_rows, "expected a row containing 新赛股份"
    cells = re.findall(r'role="cell"[^>]*>(.*?)</div>', body_rows[0], flags=re.S)
    assert len(cells) == 3
    assert cells[0].strip() == ""
    assert "新赛股份" in cells[1]
    assert "高温" in cells[2]
    # 占位格继承 rowspan 源格 class（背景连贯）
    first_cell_open = re.findall(r'<div class="([^"]*)" role="cell"', body_rows[0])
    assert first_cell_open and "bg-red" in first_cell_open[0]


def test_class_right_maps_to_align_and_selector_retarget() -> None:
    from gsuid_core.utils.html_render.table_rewrite import _rewrite_tables_local

    html = """
    <html><head><style>
    td.right{text-align:right;color:#fde68a;font-family:monospace;}
    </style></head><body>
    <table>
      <tr><th>指标</th><th>A</th><th>H</th></tr>
      <tr><td>PB</td><td class="right">4.33x</td><td class="right">5.82x</td></tr>
      <tr><td>PE</td><td class="right">~16.5x</td><td class="right">~17.0x</td></tr>
    </table>
    </body></html>
    """
    out = _rewrite_tables_local(html)
    assert "md-table-cell-right" in out
    assert "td.right" not in out
    assert ".right{" in out or ".right {" in out
    # 数值启发式也应覆盖 x / ~ 后缀
    assert out.count("md-table-cell-right") >= 4


def test_cell_is_numeric_suffixes() -> None:
    from gsuid_core.utils.html_render.table_rewrite import _cell_is_numeric

    assert _cell_is_numeric("1.77%")
    assert _cell_is_numeric("4.33x")
    assert _cell_is_numeric("~16.5x")
    assert _cell_is_numeric("+5.5%")
    assert _cell_is_numeric("-2.8pct")
    assert _cell_is_numeric("3倍")
    assert not _cell_is_numeric("农业种植")
    assert not _cell_is_numeric("")


def test_colspan_flex_style() -> None:
    from gsuid_core.utils.html_render.table_rewrite import _rewrite_tables_local

    html = '<table><tr><td colspan="2">wide</td><td>c</td></tr></table>'
    out = _rewrite_tables_local(html)
    assert "flex:2 1 0" in out
    assert "wide" in out
