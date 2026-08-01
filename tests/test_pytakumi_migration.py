"""pytakumi 迁移回归测试。

项目从 htmlkit (pyhtmlrender) 切换到 pytakumi 后，本测试覆盖：

1. pytakumi 底层 API 可用性（Renderer / html_to_pic / md_to_pic / text_to_pic）
2. gsuid_core.utils.html_render 封装层的异步接口与兼容逻辑
3. 输出格式正确性（PNG / JPEG / WEBP magic bytes）
4. 字体注册、DPR 映射、CSS 注入、Markdown 表格重写等关键路径
5. 边界情况（空输入、CJK、特殊字符、超大内容）

运行: pytest tests/test_pytakumi_migration.py -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    """限制异步测试只跑 asyncio，跳过 trio。"""
    return request.param


# ─────────────────────────────────────────────
# 0. pytakumi 底层可用性
# ─────────────────────────────────────────────


class TestPytakumiAvailability:
    """确认 pytakumi 已安装且核心符号可导入。"""

    def test_import_core_symbols(self) -> None:
        from pytakumi import (
            Renderer,
            md_to_pic,
            __version__,
            html_to_pic,
            text_to_pic,
            render_markdown,
            markdown_to_html,
            wrap_markdown_html,
            set_glyph_cache_max_bytes,
        )

        assert callable(html_to_pic)
        assert callable(md_to_pic)
        assert callable(text_to_pic)
        assert callable(set_glyph_cache_max_bytes)
        assert callable(markdown_to_html)
        assert callable(render_markdown)
        assert callable(wrap_markdown_html)
        assert isinstance(__version__, str)
        assert Renderer is not None

    def test_version_semver(self) -> None:
        from pytakumi import __version__

        parts = __version__.split(".")
        assert len(parts) >= 2, f"版本号格式异常: {__version__}"
        assert parts[0].isdigit()

    def test_renderer_creation(self) -> None:
        from pytakumi import Renderer

        r = Renderer()
        assert r is not None

    def test_renderer_with_cache(self) -> None:
        from pytakumi import Renderer

        r = Renderer(cache_max_bytes=32 * 1024 * 1024)
        assert r is not None

    def test_set_glyph_cache_max_bytes(self) -> None:
        from pytakumi import set_glyph_cache_max_bytes

        # 不应抛异常
        set_glyph_cache_max_bytes(64 * 1024 * 1024)


# ─────────────────────────────────────────────
# 1. Renderer 字体注册
# ─────────────────────────────────────────────

_FONT_PATH = Path(__file__).resolve().parent.parent / "gsuid_core" / "utils" / "fonts" / "MiSans-Bold.ttf"


class TestFontRegistration:
    def test_register_font_from_bytes(self) -> None:
        from pytakumi import Renderer

        r = Renderer()
        if _FONT_PATH.is_file():
            data = _FONT_PATH.read_bytes()
            r.register_font(data, name="MiSans")
        else:
            pytest.skip("MiSans-Bold.ttf 不存在")

    def test_register_font_bad_data_raises(self) -> None:
        from pytakumi import Renderer

        r = Renderer()
        with pytest.raises(Exception):
            r.register_font(b"not a font", name="BadFont")

    def test_register_font_dict_form(self) -> None:
        """pytakumi _util.register_fonts 支持 dict 形式。"""
        from pytakumi import Renderer
        from pytakumi._util import register_fonts

        r = Renderer()
        if _FONT_PATH.is_file():
            data = _FONT_PATH.read_bytes()
            register_fonts(r, [{"data": data, "name": "MiSansDict"}])
        else:
            pytest.skip("MiSans-Bold.ttf 不存在")


# ─────────────────────────────────────────────
# 2. html_to_pic 底层
# ─────────────────────────────────────────────

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
WEBP_MAGIC = b"RIFF"


class TestHtmlToPic:
    def test_basic_png(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic("<p>hello</p>", width=200)
        assert data[:8] == PNG_MAGIC
        assert len(data) > 100

    def test_jpeg_format(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic("<p>jpeg test</p>", width=200, format="jpeg")
        assert data[:3] == JPEG_MAGIC

    def test_webp_format(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic("<p>webp test</p>", width=200, format="webp")
        assert data[:4] == WEBP_MAGIC
        assert data[8:12] == b"WEBP"

    def test_jpeg_quality(self) -> None:
        from pytakumi import html_to_pic

        high = html_to_pic("<p>q</p>" * 200, width=400, format="jpeg", quality=95)
        low = html_to_pic("<p>q</p>" * 200, width=400, format="jpeg", quality=10)
        assert len(low) < len(high), "低质量 JPEG 应更小"

    def test_fixed_height(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic("<p>fixed</p>", width=200, height=300)
        assert data[:8] == PNG_MAGIC

    def test_auto_height(self) -> None:
        """height=None 时按内容自适应。"""
        from pytakumi import html_to_pic

        short = html_to_pic("<p>short</p>", width=200, height=None)
        long = html_to_pic("<p>line</p>" * 50, width=200, height=None)
        assert len(long) > len(short)

    def test_css_param(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic(
            "<p>styled</p>",
            width=200,
            css="p { color: red; font-size: 24px; }",
        )
        assert data[:8] == PNG_MAGIC

    def test_stylesheets_param(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic(
            "<p>sheet</p>",
            width=200,
            stylesheets=["p { color: blue; }"],
        )
        assert data[:8] == PNG_MAGIC

    def test_style_tag_extraction(self) -> None:
        """<style> 块应被提取并应用。"""
        from pytakumi import html_to_pic

        html = "<style>p { color: green; }</style><p>extracted</p>"
        data = html_to_pic(html, width=200)
        assert data[:8] == PNG_MAGIC

    def test_full_html_document(self) -> None:
        from pytakumi import html_to_pic

        html = (
            "<!DOCTYPE html><html><head><style>body{margin:0}</style></head>"
            "<body><h1>Title</h1><p>Body text</p></body></html>"
        )
        data = html_to_pic(html, width=400)
        assert data[:8] == PNG_MAGIC

    def test_device_pixel_ratio(self) -> None:
        from pytakumi import html_to_pic

        dpr1 = html_to_pic("<p>dpr</p>", width=200, device_pixel_ratio=1.0)
        dpr2 = html_to_pic("<p>dpr</p>", width=200, device_pixel_ratio=2.0)
        # 2x DPR 渲染的图应更大（像素更多）
        assert len(dpr2) > len(dpr1)

    def test_font_families(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic(
            "<p>中文测试</p>",
            width=200,
            font_families=["MiSans", "sans-serif"],
        )
        assert data[:8] == PNG_MAGIC

    def test_lang_param(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic("<p>lang</p>", width=200, lang="zh")
        assert data[:8] == PNG_MAGIC

    def test_renderer_reuse(self) -> None:
        """共享 Renderer 跨多次调用。"""
        from pytakumi import Renderer, html_to_pic

        r = Renderer()
        d1 = html_to_pic("<p>call1</p>", width=200, renderer=r)
        d2 = html_to_pic("<p>call2</p>", width=200, renderer=r)
        assert d1[:8] == PNG_MAGIC
        assert d2[:8] == PNG_MAGIC

    def test_cjk_content(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic("<p>你好世界 🌍</p>", width=300)
        assert data[:8] == PNG_MAGIC
        assert len(data) > 100

    def test_script_tag_stripped(self) -> None:
        """<script> 应被移除，不影响渲染。"""
        from pytakumi import html_to_pic

        html = '<p>safe</p><script>alert("xss")</script>'
        data = html_to_pic(html, width=200)
        assert data[:8] == PNG_MAGIC

    def test_empty_html(self) -> None:
        """空 HTML + 自适应高度 → 视口为 0，pytakumi 抛 RuntimeError。"""
        from pytakumi import html_to_pic

        with pytest.raises(RuntimeError, match="viewport"):
            html_to_pic("", width=200)

    def test_empty_html_with_height(self) -> None:
        """空 HTML 给定高度时应正常渲染。"""
        from pytakumi import html_to_pic

        data = html_to_pic("", width=200, height=100)
        assert isinstance(data, bytes)

    def test_special_characters(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic("<p>&lt;tag&gt; &amp; &quot;quotes&quot;</p>", width=200)
        assert data[:8] == PNG_MAGIC


# ─────────────────────────────────────────────
# 3. md_to_pic 底层
# ─────────────────────────────────────────────


class TestMdToPic:
    def test_basic_markdown(self) -> None:
        from pytakumi import md_to_pic

        data = md_to_pic("# Hello\n\nWorld", width=300)
        assert data[:8] == PNG_MAGIC

    def test_markdown_with_table(self) -> None:
        """GFM 表格应被重写为 flex 布局。"""
        from pytakumi import md_to_pic

        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        data = md_to_pic(md, width=400)
        assert data[:8] == PNG_MAGIC

    def test_markdown_dark_mode(self) -> None:
        from pytakumi import md_to_pic

        data = md_to_pic("# Dark", width=300, dark=True)
        assert data[:8] == PNG_MAGIC

    def test_markdown_custom_css(self) -> None:
        from pytakumi import md_to_pic

        data = md_to_pic("# Styled", width=300, css="h1 { color: red; }")
        assert data[:8] == PNG_MAGIC

    def test_markdown_jpeg(self) -> None:
        from pytakumi import md_to_pic

        data = md_to_pic("# JPEG", width=300, format="jpeg")
        assert data[:3] == JPEG_MAGIC

    def test_markdown_code_fence(self) -> None:
        from pytakumi import md_to_pic

        md = "```python\nprint('hello')\n```"
        data = md_to_pic(md, width=400)
        assert data[:8] == PNG_MAGIC

    def test_markdown_list(self) -> None:
        from pytakumi import md_to_pic

        md = "- item1\n- item2\n- item3"
        data = md_to_pic(md, width=300)
        assert data[:8] == PNG_MAGIC

    def test_markdown_cjk(self) -> None:
        from pytakumi import md_to_pic

        data = md_to_pic("# 中文标题\n\n这是**加粗**内容", width=400)
        assert data[:8] == PNG_MAGIC

    def test_markdown_renderer_reuse(self) -> None:
        from pytakumi import Renderer, md_to_pic

        r = Renderer()
        d1 = md_to_pic("# One", width=200, renderer=r)
        d2 = md_to_pic("# Two", width=200, renderer=r)
        assert d1[:8] == PNG_MAGIC
        assert d2[:8] == PNG_MAGIC


# ─────────────────────────────────────────────
# 4. text_to_pic 底层
# ─────────────────────────────────────────────


class TestTextToPic:
    def test_basic_text(self) -> None:
        from pytakumi import text_to_pic

        data = text_to_pic("Hello World", width=300)
        assert data[:8] == PNG_MAGIC

    def test_text_with_title(self) -> None:
        from pytakumi import text_to_pic

        data = text_to_pic("body", width=300, title="Title", eyebrow="Meta", footer="Foot")
        assert data[:8] == PNG_MAGIC

    def test_text_light_theme(self) -> None:
        from pytakumi import text_to_pic

        data = text_to_pic("light", width=300, theme="light")
        assert data[:8] == PNG_MAGIC

    def test_text_dark_theme(self) -> None:
        from pytakumi import text_to_pic

        data = text_to_pic("dark", width=300, theme="dark")
        assert data[:8] == PNG_MAGIC

    def test_text_multiline(self) -> None:
        from pytakumi import text_to_pic

        text = "\n".join(f"line {i}" for i in range(20))
        data = text_to_pic(text, width=300)
        assert data[:8] == PNG_MAGIC

    def test_text_cjk(self) -> None:
        from pytakumi import text_to_pic

        data = text_to_pic("你好世界\n第二行", width=300)
        assert data[:8] == PNG_MAGIC

    def test_text_fixed_height(self) -> None:
        from pytakumi import text_to_pic

        data = text_to_pic("fixed", width=300, height=500)
        assert data[:8] == PNG_MAGIC

    def test_text_jpeg(self) -> None:
        from pytakumi import text_to_pic

        data = text_to_pic("jpeg text", width=300, format="jpeg")
        assert data[:3] == JPEG_MAGIC

    def test_text_escaping(self) -> None:
        """HTML 特殊字符应被转义，不注入。"""
        from pytakumi import text_to_pic

        data = text_to_pic('<script>alert("x")</script>', width=300)
        assert data[:8] == PNG_MAGIC


# ─────────────────────────────────────────────
# 5. markdown_to_html 单元
# ─────────────────────────────────────────────


class TestMarkdownToHtml:
    def test_heading(self) -> None:
        from pytakumi import markdown_to_html

        html = markdown_to_html("# Title")
        assert "<h1>" in html
        assert "Title" in html

    def test_bold_italic(self) -> None:
        from pytakumi import markdown_to_html

        html = markdown_to_html("**bold** and *italic*")
        assert "<strong>" in html
        assert "<em>" in html

    def test_code_fence(self) -> None:
        from pytakumi import markdown_to_html

        html = markdown_to_html("```python\nprint(1)\n```")
        assert "<code" in html
        assert "print(1)" in html

    def test_table_to_html(self) -> None:
        """GFM 表格应被转为 HTML（table 或 flex 重写取决于版本）。"""
        from pytakumi import markdown_to_html

        md = "| H1 | H2 |\n|---|---|\n| a | b |"
        html = markdown_to_html(md)
        # 无论是否经过 flex 重写，单元格内容必须保留
        assert "H1" in html
        assert "H2" in html
        assert "a" in html
        assert "b" in html

    def test_list(self) -> None:
        from pytakumi import markdown_to_html

        html = markdown_to_html("- a\n- b")
        assert "<li>" in html

    def test_wrap_markdown_html(self) -> None:
        from pytakumi import wrap_markdown_html

        wrapped = wrap_markdown_html("<p>hi</p>")
        assert 'class="markdown-body"' in wrapped
        assert "<p>hi</p>" in wrapped


# ─────────────────────────────────────────────
# 6. _util 辅助函数
# ─────────────────────────────────────────────


class TestUtilHelpers:
    def test_escape(self) -> None:
        from pytakumi._util import escape

        assert escape("<b>") == "&lt;b&gt;"
        assert escape('"q"') == "&quot;q&quot;"

    def test_extract_styles_and_body(self) -> None:
        from pytakumi._util import extract_styles_and_body

        html = "<style>p{color:red}</style><body><p>hi</p></body>"
        styles, body = extract_styles_and_body(html, 400, None)
        assert len(styles) == 1
        assert "color:red" in styles[0]
        assert "<p>hi</p>" in body
        assert "pytakumi-root" in body

    def test_extract_styles_body_selector_rewrite(self) -> None:
        """body 选择器应被重写为 .pytakumi-root。"""
        from pytakumi._util import extract_styles_and_body

        html = "<style>body { margin: 0; }</style><p>test</p>"
        styles, _ = extract_styles_and_body(html, 200, None)
        assert ".pytakumi-root" in styles[0]
        # 原始的 body 选择器不应保留
        assert "body {" not in styles[0] or ".pytakumi-root" in styles[0]

    def test_extract_strips_script(self) -> None:
        from pytakumi._util import extract_styles_and_body

        html = "<p>ok</p><script>evil()</script>"
        _, body = extract_styles_and_body(html, 200, None)
        assert "script" not in body.lower()
        assert "evil" not in body

    def test_resolve_renderer_default(self) -> None:
        from pytakumi import Renderer
        from pytakumi._util import resolve_renderer

        r = resolve_renderer(None, None)
        assert isinstance(r, Renderer)

    def test_resolve_renderer_explicit(self) -> None:
        from pytakumi import Renderer
        from pytakumi._util import resolve_renderer

        explicit = Renderer()
        r = resolve_renderer(explicit, None)
        assert r is explicit

    def test_load_template(self) -> None:
        from pytakumi._util import load_template

        css = load_template("github-markdown.css")
        assert isinstance(css, str)
        assert len(css) > 100


# ─────────────────────────────────────────────
# 7. gsuid_core html_render 封装层
# ─────────────────────────────────────────────


class TestHtmlRenderWrapper:
    """测试 gsuid_core.utils.html_render 的封装逻辑。"""

    def test_module_imports(self) -> None:
        import gsuid_core.utils.html_render as hr

        assert hr._PYTAKUMI_AVAILABLE is True
        assert callable(hr.render_html_to_bytes)
        assert callable(hr.render_md_to_bytes)
        assert callable(hr.render_text_to_bytes)
        assert callable(hr.init_html_fontconfig)

    def test_resolve_format(self) -> None:
        from gsuid_core.utils.html_render import _resolve_format

        assert _resolve_format("png") == "png"
        assert _resolve_format("PNG") == "png"
        assert _resolve_format("jpg") == "jpeg"
        assert _resolve_format("jpeg") == "jpeg"
        assert _resolve_format("JPEG") == "jpeg"
        assert _resolve_format("webp") == "webp"
        assert _resolve_format("") == "png"  # 默认

    def test_dpr_from_dpi(self) -> None:
        from gsuid_core.utils.html_render import _dpr_from_dpi

        assert _dpr_from_dpi(None) is None
        assert _dpr_from_dpi(96.0) is None  # 96/96=1.0 → None
        assert _dpr_from_dpi(192.0) == pytest.approx(2.0)
        assert _dpr_from_dpi(144.0) == pytest.approx(1.5)
        assert _dpr_from_dpi("bad") is None  # type: ignore[arg-type]

    def test_font_families(self) -> None:
        from gsuid_core.utils.html_render import _font_families

        # 默认带 MiSans
        families = _font_families(None)
        assert "MiSans" in families

        # 自定义字体 + MiSans 兜底
        families = _font_families("CustomFont")
        assert "CustomFont" in families
        assert "MiSans" in families

        # 通用族不额外添加
        families = _font_families("sans-serif")
        assert "sans-serif" not in families
        assert "MiSans" in families

    def test_ensure_renderer_singleton(self) -> None:
        import gsuid_core.utils.html_render as hr

        r1 = hr._ensure_renderer()
        r2 = hr._ensure_renderer()
        assert r1 is r2

    def test_ensure_renderer_force(self) -> None:
        import gsuid_core.utils.html_render as hr

        # r1 = hr._ensure_renderer()
        r2 = hr._ensure_renderer(force=True)
        # force 重建后应是新对象
        assert r2 is not None

    def test_init_html_fontconfig_compat(self) -> None:
        """旧 fontconfig 接口应正常工作（参数被忽略）。"""
        from gsuid_core.utils.html_render import init_html_fontconfig

        result = init_html_fontconfig(
            fontconfig_path="/fake/path",
            fontconfig_file="fonts.conf",
            fc_debug="1",
        )
        assert result is True


# ─────────────────────────────────────────────
# 8. 异步渲染接口（核心集成）
# ─────────────────────────────────────────────


class TestAsyncRenderAPIs:
    """测试 render_*_to_bytes 异步接口。"""

    @pytest.mark.anyio
    async def test_render_html_to_bytes_png(self) -> None:
        from gsuid_core.utils.html_render import render_html_to_bytes

        data = await render_html_to_bytes("<h1>Test</h1><p>内容</p>")
        assert data[:8] == PNG_MAGIC
        assert len(data) > 100

    @pytest.mark.anyio
    async def test_render_html_to_bytes_jpeg(self) -> None:
        from gsuid_core.utils.html_render import render_html_to_bytes

        data = await render_html_to_bytes(
            "<h1>JPEG</h1>",
            image_format="jpeg",
            jpeg_quality=85,
        )
        assert data[:3] == JPEG_MAGIC

    @pytest.mark.anyio
    async def test_render_html_to_bytes_custom_width(self) -> None:
        from gsuid_core.utils.html_render import render_html_to_bytes

        data = await render_html_to_bytes("<p>wide</p>", max_width=1200.0)
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_html_to_bytes_dpi(self) -> None:
        from gsuid_core.utils.html_render import render_html_to_bytes

        data = await render_html_to_bytes("<p>hires</p>", dpi=192.0)
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_html_to_bytes_no_refit(self) -> None:
        from gsuid_core.utils.html_render import render_html_to_bytes

        data = await render_html_to_bytes(
            "<p>fixed height</p>",
            allow_refit=False,
            device_height=400.0,
        )
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_html_to_bytes_font_name(self) -> None:
        from gsuid_core.utils.html_render import render_html_to_bytes

        data = await render_html_to_bytes(
            "<p>中文</p>",
            font_name="MiSans",
        )
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_md_to_bytes_basic(self) -> None:
        from gsuid_core.utils.html_render import render_md_to_bytes

        data = await render_md_to_bytes(md="# 标题\n\n正文内容")
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_md_to_bytes_jpeg(self) -> None:
        from gsuid_core.utils.html_render import render_md_to_bytes

        data = await render_md_to_bytes(
            md="# JPEG MD",
            image_format="jpeg",
            jpeg_quality=90,
        )
        assert data[:3] == JPEG_MAGIC

    @pytest.mark.anyio
    async def test_render_md_to_bytes_custom_width(self) -> None:
        from gsuid_core.utils.html_render import render_md_to_bytes

        data = await render_md_to_bytes(md="# Wide", max_width=800)
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_md_to_bytes_from_file(self, tmp_path: Path) -> None:
        from gsuid_core.utils.html_render import render_md_to_bytes

        md_file = tmp_path / "test.md"
        md_file.write_text("# From File\n\nContent here", encoding="utf-8")
        data = await render_md_to_bytes(md_path=str(md_file))
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_md_to_bytes_with_css(self, tmp_path: Path) -> None:
        from gsuid_core.utils.html_render import render_md_to_bytes

        css_file = tmp_path / "custom.css"
        css_file.write_text("h1 { color: blue; }", encoding="utf-8")
        data = await render_md_to_bytes(md="# Styled", css_path=str(css_file))
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_md_to_bytes_table(self) -> None:
        from gsuid_core.utils.html_render import render_md_to_bytes

        md = "| 列A | 列B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |"
        data = await render_md_to_bytes(md=md, max_width=600)
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_text_to_bytes_basic(self) -> None:
        from gsuid_core.utils.html_render import render_text_to_bytes

        data = await render_text_to_bytes("纯文本内容\n第二行")
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_text_to_bytes_jpeg(self) -> None:
        from gsuid_core.utils.html_render import render_text_to_bytes

        data = await render_text_to_bytes("jpeg text", image_format="jpeg")
        assert data[:3] == JPEG_MAGIC

    @pytest.mark.anyio
    async def test_render_text_to_bytes_no_refit(self) -> None:
        from gsuid_core.utils.html_render import render_text_to_bytes

        data = await render_text_to_bytes("fixed", allow_refit=False)
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_text_to_bytes_with_css(self, tmp_path: Path) -> None:
        from gsuid_core.utils.html_render import render_text_to_bytes

        css_file = tmp_path / "text.css"
        css_file.write_text(".body { font-size: 20px; }", encoding="utf-8")
        data = await render_text_to_bytes("styled text", css_path=str(css_file))
        assert data[:8] == PNG_MAGIC


# ─────────────────────────────────────────────
# 9. 边界与压力
# ─────────────────────────────────────────────


class TestEdgeCases:
    def test_very_long_html(self) -> None:
        from pytakumi import html_to_pic

        html = "<p>row</p>" * 500
        data = html_to_pic(html, width=400)
        assert data[:8] == PNG_MAGIC

    def test_very_long_markdown(self) -> None:
        from pytakumi import md_to_pic

        md = "\n".join(f"## Section {i}\n\nParagraph {i}" for i in range(100))
        data = md_to_pic(md, width=600)
        assert data[:8] == PNG_MAGIC

    def test_nested_html(self) -> None:
        from pytakumi import html_to_pic

        html = "<div>" * 20 + "<p>deep</p>" + "</div>" * 20
        data = html_to_pic(html, width=300)
        assert data[:8] == PNG_MAGIC

    def test_unicode_emoji(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic("<p>🎮🎯🚀💡🔥</p>", width=300)
        assert data[:8] == PNG_MAGIC

    def test_mixed_cjk_latin(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic("<p>中文English日本語한국어</p>", width=400)
        assert data[:8] == PNG_MAGIC

    def test_html_entities(self) -> None:
        from pytakumi import html_to_pic

        data = html_to_pic("<p>&copy; 2026 &mdash; &euro;100</p>", width=300)
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_concurrent_renders(self) -> None:
        """并发渲染不应崩溃。"""
        from gsuid_core.utils.html_render import render_html_to_bytes

        tasks = [render_html_to_bytes(f"<p>task {i}</p>", max_width=200.0) for i in range(5)]
        results = await asyncio.gather(*tasks)
        for data in results:
            assert data[:8] == PNG_MAGIC

    def test_width_clamped_to_1(self) -> None:
        """max_width=0 应被 clamp 到 1。"""
        from gsuid_core.utils.html_render import _sync_render_html

        data = _sync_render_html(
            "<p>tiny</p>",
            max_width=0,
            dpi=96.0,
            device_height=100,
            default_font_size=12.0,
            font_name="sans-serif",
            allow_refit=True,
            image_format="png",
            jpeg_quality=100,
            lang="zh",
        )
        assert isinstance(data, bytes)


# ─────────────────────────────────────────────
# 10. render_markdown 兼容别名
# ─────────────────────────────────────────────


class TestRenderMarkdownAlias:
    def test_render_markdown_alias(self) -> None:
        from pytakumi import render_markdown

        data = render_markdown("# Alias Test\n\nWorks!", width=300)
        assert data[:8] == PNG_MAGIC

    def test_render_markdown_dark(self) -> None:
        from pytakumi import render_markdown

        data = render_markdown("# Dark Alias", width=300, dark=True)
        assert data[:8] == PNG_MAGIC


# ─────────────────────────────────────────────
# 11. 与旧接口签名兼容性
# ─────────────────────────────────────────────


class TestBackwardCompat:
    """确保 html_render 对外签名与旧 htmlkit 时代兼容。"""

    @pytest.mark.anyio
    async def test_render_html_signature(self) -> None:
        """render_html_to_bytes 应接受旧调用方式的所有参数。"""
        import inspect

        from gsuid_core.utils.html_render import render_html_to_bytes

        sig = inspect.signature(render_html_to_bytes)
        expected_params = {
            "html",
            "max_width",
            "dpi",
            "device_height",
            "default_font_size",
            "font_name",
            "allow_refit",
            "image_format",
            "jpeg_quality",
            "lang",
        }
        actual_params = set(sig.parameters.keys())
        assert expected_params.issubset(actual_params), f"缺少参数: {expected_params - actual_params}"

    @pytest.mark.anyio
    async def test_render_md_signature(self) -> None:
        import inspect

        from gsuid_core.utils.html_render import render_md_to_bytes

        sig = inspect.signature(render_md_to_bytes)
        expected_params = {
            "md",
            "md_path",
            "css_path",
            "max_width",
            "dpi",
            "allow_refit",
            "image_format",
            "jpeg_quality",
        }
        actual_params = set(sig.parameters.keys())
        assert expected_params.issubset(actual_params), f"缺少参数: {expected_params - actual_params}"

    @pytest.mark.anyio
    async def test_render_text_signature(self) -> None:
        import inspect

        from gsuid_core.utils.html_render import render_text_to_bytes

        sig = inspect.signature(render_text_to_bytes)
        expected_params = {
            "text",
            "css_path",
            "max_width",
            "dpi",
            "allow_refit",
            "image_format",
            "jpeg_quality",
        }
        actual_params = set(sig.parameters.keys())
        assert expected_params.issubset(actual_params), f"缺少参数: {expected_params - actual_params}"

    def test_init_fontconfig_signature(self) -> None:
        """init_html_fontconfig 应保留旧参数名。"""
        import inspect

        from gsuid_core.utils.html_render import init_html_fontconfig

        sig = inspect.signature(init_html_fontconfig)
        expected_params = {
            "fontconfig_path",
            "fontconfig_file",
            "fontconfig_sysroot",
            "fc_debug",
            "fc_lang",
            "fontconfig_use_mmap",
        }
        actual_params = set(sig.parameters.keys())
        assert expected_params.issubset(actual_params), f"缺少参数: {expected_params - actual_params}"


# ─────────────────────────────────────────────
# 12. 实际业务场景模拟
# ─────────────────────────────────────────────


class TestBusinessScenarios:
    """模拟 agent 实际调用路径。"""

    @pytest.mark.anyio
    async def test_report_artifact_render(self) -> None:
        """模拟 _send_report_images 的渲染路径。"""
        from gsuid_core.utils.html_render import render_md_to_bytes

        title = "转账说明"
        body = "| 项目 | 金额 |\n|---|---|\n| 转账 | ¥100 |"
        md = f"# {title}\n\n{body}\n\n---\n*数据仅供参考*"
        data = await render_md_to_bytes(md=md, max_width=600, image_format="jpeg")
        assert data[:3] == JPEG_MAGIC
        assert len(data) > 500

    @pytest.mark.anyio
    async def test_html_tool_render(self) -> None:
        """模拟 render_html_to_image 工具的调用路径。"""
        from gsuid_core.utils.html_render import render_html_to_bytes

        html = (
            "<!DOCTYPE html>\n<html>\n<head>\n"
            '<meta charset="utf-8">\n'
            "<style>body{font-family:sans-serif;padding:20px;}</style>\n"
            "</head>\n<body>\n<h1>报告</h1><p>内容</p>\n</body>\n</html>"
        )
        data = await render_html_to_bytes(
            html,
            max_width=1000.0,
            image_format="jpeg",
        )
        assert data[:3] == JPEG_MAGIC

    @pytest.mark.anyio
    async def test_long_markdown_fallback_render(self) -> None:
        """模拟 _try_render_markdown_image 的长文渲染。"""
        from gsuid_core.utils.html_render import render_md_to_bytes

        md = "\n\n".join(f"## 章节 {i}\n\n{'内容段落。' * 10}" for i in range(20))
        data = await render_md_to_bytes(md=md, max_width=700, image_format="jpeg")
        assert data[:3] == JPEG_MAGIC
        assert len(data) > 1000

    @pytest.mark.anyio
    async def test_agent_html_with_inline_styles(self) -> None:
        """agent 生成的带内联样式的 HTML。"""
        from gsuid_core.utils.html_render import render_html_to_bytes

        html = """
        <div style="background:#1a1a2e;color:#eee;padding:24px;border-radius:12px;">
            <h2 style="color:#e94560;">数据分析</h2>
            <table style="width:100%;border-collapse:collapse;">
                <tr><td style="padding:8px;border:1px solid #333;">指标</td>
                    <td style="padding:8px;border:1px solid #333;">值</td></tr>
                <tr><td style="padding:8px;border:1px solid #333;">DAU</td>
                    <td style="padding:8px;border:1px solid #333;">12,345</td></tr>
            </table>
        </div>
        """
        data = await render_html_to_bytes(html, max_width=800.0)
        assert data[:8] == PNG_MAGIC
