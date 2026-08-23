"""IM 场景 HTML 模板库（im_templates）回归测试。

覆盖：

1. 8 个 ``*_card`` HTML 生成函数的结构与内容正确性
2. HTML 转义（防注入）
3. 对比表 ✓/✕ 自动着色语义（只着色无歧义符号，「是/否」不着色）
4. MiSans 缺字符号归一化（✗ → ✕，避免渲染成空白）
5. 等宽字体（Mono）注册与代码卡片引用
6. 8 个 ``render_*_card`` 异步接口真实渲染出合法 PNG

运行: pytest tests/test_im_templates.py -v
"""

from __future__ import annotations

import pytest

from gsuid_core.utils.html_render import im_templates as T

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    """异步测试只跑 asyncio。"""
    return request.param


# ─────────────────────────────────────────────
# HTML 生成：结构与内容
# ─────────────────────────────────────────────


class TestHtmlGeneration:
    def test_summary_card_structure(self) -> None:
        html = T.summary_card(
            "今日要点",
            ["第一件事", "第二件事"],
            eyebrow="RECAP",
            footer="Mavis",
        )
        assert "<!DOCTYPE html>" in html
        assert "今日要点" in html
        assert "第一件事" in html
        assert "第二件事" in html
        assert "RECAP" in html
        assert "Mavis" in html
        # 每个要点一个圆点
        assert html.count('class="dot"') == 2

    def test_ranking_card_medals(self) -> None:
        html = T.ranking_card(
            "排行",
            [("A", "1"), ("B", "2"), ("C", "3"), ("D", "4")],
        )
        # 前三名用奖牌色，第四名用普通底色
        assert T._MEDALS[0] in html  # 金
        assert T._MEDALS[1] in html  # 银
        assert T._MEDALS[2] in html  # 铜
        assert ">1<" in html and ">4<" in html

    def test_comparison_card_columns(self) -> None:
        html = T.comparison_card(
            "对比",
            ["维度", "方案A", "方案B"],
            [["速度", "快", "慢"], ["成本", "低", "高"]],
        )
        for token in ("维度", "方案A", "方案B", "速度", "成本"):
            assert token in html
        # 表头行 + 2 数据行
        assert html.count('class="trow') == 3

    def test_comparison_card_ragged_row(self) -> None:
        """某行单元格少于表头列数时不应崩溃，缺的补空。"""
        html = T.comparison_card("对比", ["A", "B", "C"], [["只有1列"]])
        assert "只有1列" in html

    def test_quote_card(self) -> None:
        html = T.quote_card("简单是可靠的先决条件。", attribution="Dijkstra")
        assert "简单是可靠的先决条件。" in html
        assert "Dijkstra" in html
        assert "&ldquo;" in html  # 装饰引号

    def test_metrics_card_deltas(self) -> None:
        html = T.metrics_card(
            "指标",
            [("100", "消息"), ("5", "用户")],
            deltas=["+10%", "-2%"],
        )
        assert "100" in html and "消息" in html
        # 正负增减幅分别着绿/红
        assert f"color:{T._TEAL}" in html
        assert f"color:{T._CORAL}" in html

    def test_notice_card_kinds(self) -> None:
        for kind, accent in (
            ("info", T._BLUE),
            ("success", T._TEAL),
            ("warning", T._GOLD),
            ("danger", T._CORAL),
        ):
            html = T.notice_card("标题", "正文", kind=kind)
            assert accent in html, f"kind={kind} 应使用主色 {accent}"

    def test_steps_card_connectors(self) -> None:
        html = T.steps_card("步骤", [("一", "说明1"), ("二", "说明2"), ("三", "说明3")])
        # 最后一步没有连接线，前两步有
        assert html.count('class="connector"') == 2
        assert ">1<" in html and ">3<" in html

    def test_code_card_chrome(self) -> None:
        html = T.code_card(
            "print('hi')",
            language="PYTHON",
            filename="demo.py",
        )
        assert "print(&#x27;hi&#x27;)" in html or "print('hi')" in html.replace("&#x27;", "'")
        assert "demo.py" in html
        assert "PYTHON" in html
        # 三点窗控
        assert html.count('class="tdot"') == 3
        # 代码块引用等宽字体栈
        assert '"Mono"' in html


# ─────────────────────────────────────────────
# 转义（防注入）
# ─────────────────────────────────────────────


class TestEscaping:
    @pytest.mark.parametrize(
        "fn,args",
        [
            (T.summary_card, ("<script>alert(1)</script>", ["<b>x</b>"])),
            (T.quote_card, ("<img src=x onerror=alert(1)>",)),
            (T.notice_card, ("<script>", "<b>body</b>")),
            (T.code_card, ("<script>alert('x')</script>",)),
        ],
    )
    def test_no_raw_script_injection(self, fn, args) -> None:
        html = fn(*args)
        # 用户内容中的 <script> 必须被转义，不能以原始标签出现
        assert "<script>alert" not in html

    def test_summary_escapes_points(self) -> None:
        html = T.summary_card("t", ["<b>bold</b>"])
        assert "<b>bold</b>" not in html
        assert "&lt;b&gt;bold&lt;/b&gt;" in html


# ─────────────────────────────────────────────
# 对比表着色语义 + 符号归一化
# ─────────────────────────────────────────────


class TestComparisonColoring:
    def test_check_symbol_positive(self) -> None:
        assert T._cell_style("✓") == f"color:{T._TEAL};font-weight:700;"

    def test_cross_symbol_negative(self) -> None:
        assert T._cell_style("✕") == f"color:{T._CORAL};font-weight:700;"
        assert T._cell_style("×") == f"color:{T._CORAL};font-weight:700;"

    @pytest.mark.parametrize("word", ["是", "否", "支持", "不支持", "yes", "no", "有", "无"])
    def test_yes_no_words_not_colored(self, word: str) -> None:
        """「是/否」类词语无好坏含义，不应自动着色。"""
        assert T._cell_style(word) == ""

    def test_plain_text_not_colored(self) -> None:
        assert T._cell_style("快") == ""
        assert T._cell_style("2-5s") == ""

    def test_glyph_normalization(self) -> None:
        """MiSans 缺字的 ✗/✘ 应被归一化为 ✕。"""
        assert T._norm_glyphs("✗") == "✕"
        assert T._norm_glyphs("✘") == "✕"
        assert T._norm_glyphs("✓") == "✓"  # 已支持的不变

    def test_comparison_normalizes_missing_glyph_cross(self) -> None:
        """传入 ✗（MiSans 缺字）时，渲染出的 HTML 应为可渲染的 ✕。"""
        html = T.comparison_card("对比", ["A", "B"], [["行", "✗", "✓"]])
        assert "✗" not in html  # 缺字符号不应原样保留
        assert "✕" in html  # 归一化后可渲染
        # 且该 ✕ 被着红色
        assert f'color:{T._CORAL};font-weight:700;">✕' in html.replace(" ", "")


# ─────────────────────────────────────────────
# 等宽字体注册
# ─────────────────────────────────────────────


class TestMonoFont:
    def test_mono_font_registered(self) -> None:
        """共享渲染器应注册了等宽字体（本机有 Consolas/Menlo/DejaVu 之一）。"""
        import gsuid_core.utils.html_render as hr

        renderer = hr._ensure_renderer(force=True)
        assert renderer is not None
        # _find_mono_font 在本机应能找到至少一个等宽字体
        mono = hr._find_mono_font()
        # 若本机确实没有任何等宽字体则跳过（不强制失败，保证可移植）
        if mono is None:
            pytest.skip("本机未找到任何等宽字体，代码卡片将回退 MiSans")
        assert isinstance(mono, bytes) and len(mono) > 0

    def test_code_card_uses_mono_stack(self) -> None:
        html = T.code_card("x = 1")
        # pre 块必须引用 Mono 字体栈
        assert '"Mono"' in html


class TestEmojiFont:
    def test_bundled_colr_font_exists(self) -> None:
        import gsuid_core.utils.html_render as hr

        assert hr._BUNDLED_EMOJI_FONT.is_file()
        data = hr._find_emoji_font()
        assert data is not None and len(data) > 1000

    def test_emoji_font_registered(self) -> None:
        import gsuid_core.utils.html_render as hr

        hr._ensure_renderer(force=True)
        data = hr._find_emoji_font()
        if data is None:
            pytest.skip("未找到 emoji COLR 字体")
        assert hr._EMOJI_FONT_NAME in hr._font_families(None)

    @pytest.mark.anyio
    async def test_emoji_renders_chromatic_ink(self) -> None:
        """☔ 走 COLR 回退脸，不应是无彩度的空心方框。"""
        import io

        from PIL import Image

        import gsuid_core.utils.html_render as hr

        hr._ensure_renderer(force=True)
        html = (
            '<div style="padding:20px;background:#eef4fb;color:#1a2332;'
            'font-family:MiSans,sans-serif;font-size:40px">☔⚠📌你好</div>'
        )
        png = await hr.render_html_to_bytes(html, max_width=480, dpi=96)
        assert png[:8] == PNG_MAGIC
        img = Image.open(io.BytesIO(png)).convert("RGB")
        chromatic = 0
        for r, g, b in img.getdata():
            if max(r, g, b) - min(r, g, b) > 40:
                chromatic += 1
        assert chromatic > 80, f"emoji 无彩色墨迹（chroma_pixels={chromatic}），仍是豆腐"


# ─────────────────────────────────────────────
# 真实渲染：8 个异步接口
# ─────────────────────────────────────────────


class TestRendering:
    @pytest.mark.anyio
    async def test_render_summary(self) -> None:
        data = await T.render_summary_card("标题", ["要点一", "要点二"], footer="f")
        assert data[:8] == PNG_MAGIC and len(data) > 500

    @pytest.mark.anyio
    async def test_render_ranking(self) -> None:
        data = await T.render_ranking_card("榜", [("甲", "1"), ("乙", "2")])
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_comparison(self) -> None:
        data = await T.render_comparison_card("对比", ["维度", "A", "B"], [["速度", "✗", "✓"]])
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_quote(self) -> None:
        data = await T.render_quote_card("一句话", attribution="某人")
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_metrics(self) -> None:
        data = await T.render_metrics_card("指标", [("1", "a"), ("2", "b")], deltas=["+1%", "-1%"])
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_notice(self) -> None:
        data = await T.render_notice_card("通知", "内容", tag="公告", kind="warning")
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_steps(self) -> None:
        data = await T.render_steps_card("教程", [("步骤1", "说明"), ("步骤2", "说明")])
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_code(self) -> None:
        data = await T.render_code_card("print('hello')", language="PYTHON")
        assert data[:8] == PNG_MAGIC

    @pytest.mark.anyio
    async def test_render_cjk_not_tofu(self) -> None:
        """中文内容渲染后字节数应明显大于空白图（间接证明不是缺字豆腐块）。"""
        rich = await T.render_summary_card("中文标题", ["中文要点内容，包含标点符号。"] * 4)
        assert len(rich) > 3000  # 有实际字形渲染，字节数可观
