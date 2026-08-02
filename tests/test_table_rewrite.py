"""table_rewrite：简单表改写 + 嵌套表原样保留 + class 保留 + 主题 CSS。"""

from __future__ import annotations


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
