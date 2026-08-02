"""render_html_to_image 默认自由 HTML：不套设计壳；原生 table 改写。"""

from __future__ import annotations


def test_prepare_free_html_does_not_wrap_design_shell() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _prepare_free_html

    frag = '<div class="grid"><div class="metric">x</div></div>'
    out = _prepare_free_html(frag)
    # 注入引擎卫生 CSS，但不套暗色设计壳
    assert frag in out or "grid" in out
    assert "engine hygiene" in out
    assert "linear-gradient(175deg" not in out
    assert "AI 生成资料" not in out


def test_prepare_free_html_passes_full_document() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _prepare_free_html

    full = "<!DOCTYPE html><html><body><p>hi</p></body></html>"
    out = _prepare_free_html(full)
    assert "<p>hi</p>" in out
    assert "engine hygiene" in out
    assert "linear-gradient(175deg" not in out


def test_prepare_free_html_rewrites_native_table() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _prepare_free_html

    tbl = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    rw = _prepare_free_html(tbl)
    assert "md-table" in rw
    assert "md-table-row" in rw
    assert "md-table-cell" in rw
    # flex 最小样式应注入（自由 HTML 无 github 主题）
    assert ".md-table" in rw or "md-table{" in rw.replace(" ", "")


def test_prepare_free_html_dark_page_keeps_td_class_and_layout_css() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _prepare_free_html

    html = """<!DOCTYPE html><html><head><style>
body{background:#0f172a;color:#e2e8f0;} .up{color:#34d399;}
</style></head><body>
<table><tr><td class="up">+5%</td></tr></table>
</body></html>"""
    rw = _prepare_free_html(html)
    assert "up" in rw
    assert "md-table" in rw
    # layout-only：不注入浅色/深色硬表底字色，避免盖掉 .up
    assert "#eef3f9" not in rw
    assert "#1a2538" not in rw
    assert "rgba(128,128,128" in rw.replace(" ", "") or "display:flex" in rw.replace(" ", "")


def test_design_shell_still_available_explicitly() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import (
        _wrap_html_if_needed,
        _wrap_with_design_shell,
    )

    frag = "<h1>title</h1>"
    shelled = _wrap_with_design_shell(frag)
    assert "linear-gradient(175deg" in shelled
    assert "title" in shelled
    assert _wrap_html_if_needed is _wrap_with_design_shell


def test_full_html_skips_design_shell() -> None:
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _wrap_with_design_shell

    full = "<!DOCTYPE html><html><body><p>keep</p></body></html>"
    assert _wrap_with_design_shell(full) == full
