"""IM 场景 HTML 图片模板库（基于 pytakumi）。

为 agent「生成 HTML → 渲染成图片 → 发送到 IM」这条链路提供一组开箱即用、
针对聊天窗口阅读场景优化过的卡片模板。

设计约束（由 pytakumi 引擎能力决定，非浏览器）：

- 布局只用 flexbox（引擎不支持 ``display: table``，表格一律用 flex 行模拟）。
- 中文必须走注册了 MiSans 的共享 Renderer，因此渲染统一走
  :func:`gsuid_core.utils.html_render.render_html_to_bytes`。
- 高度全部自适应（``allow_refit=True``），卡片随内容伸缩，适合 IM 竖屏阅读。
- 深色底 + 大字号 + 强字重对比，保证手机小图也清晰可读。

每个 ``*_card`` 函数返回可直接渲染的 HTML 字符串；配套的 ``render_*_card``
异步函数直接返回图片字节。所有用户内容均经过 HTML 转义。

用法::

    from gsuid_core.utils.html_render.im_templates import render_summary_card

    img = await render_summary_card(
        eyebrow="每日复盘",
        title="今日要点",
        points=["完成了 pytakumi 迁移", "测试全部通过"],
        footer="Mavis · 2026-07-28",
    )
"""

from __future__ import annotations

import html as _html
from typing import Sequence

from gsuid_core.utils.html_render import render_html_to_bytes

__all__ = [
    # HTML 生成
    "summary_card",
    "ranking_card",
    "comparison_card",
    "quote_card",
    "metrics_card",
    "notice_card",
    "steps_card",
    "code_card",
    # 直接渲染为字节
    "render_summary_card",
    "render_ranking_card",
    "render_comparison_card",
    "render_quote_card",
    "render_metrics_card",
    "render_notice_card",
    "render_steps_card",
    "render_code_card",
]


# ─────────────────────────────────────────────
# 设计系统
# ─────────────────────────────────────────────
#
# 色板刻意避开「近黑 + 单一霓虹」的套路：底色是带蓝调的墨色渐变，
# 每张卡片有自己的主色，但共享同一套中性色与结构，保证成系列感。

_BG = "linear-gradient(180deg,#101828 0%,#141d30 100%)"
_SURFACE = "#1b2537"
_SURFACE_ALT = "#202c42"
_LINE = "#2b3850"
_TEXT = "#e9eef7"
_TEXT_DIM = "#9aa7bd"
_TEXT_FAINT = "#6b7890"

# 各模板主色
_GOLD = "#e8b45a"
_TEAL = "#3ec9a7"
_CORAL = "#ef7d6c"
_BLUE = "#5b9dd9"
_VIOLET = "#9d8cff"

# 排行榜前三名奖牌色
_MEDALS = ("#e8b45a", "#c3cdd9", "#d09a6a")

_FONT = '"MiSans","PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif'
# "Mono" 是 html_render._ensure_renderer 注册的等宽字体（Consolas/Menlo/
# DejaVu 等，找不到时回退 MiSans）。代码块务必引用它。
_MONO = '"Mono","JetBrains Mono",Consolas,Menlo,monospace'

# 卡片统一宽度（IM 竖屏友好），高度自适应
CARD_WIDTH = 720


def _e(value: object) -> str:
    """转义用户内容。"""
    return _html.escape(str(value), quote=True)


def _page(style: str, body: str) -> str:
    """把 <style> + 内容组装成完整 HTML 文档。

    pytakumi 会抽取 <style> 并把 ``body`` 选择器重写为根容器，
    因此这里用标准 document 结构即可。
    """
    return (
        "<!DOCTYPE html>\n"
        '<html><head><meta charset="utf-8">\n'
        f"<style>{style}</style>\n"
        f"</head><body>{body}</body></html>"
    )


# 所有卡片共享的根样式骨架。{accent} 由具体模板填入。
_BASE = """
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{
  width:100%;
  background:{bg};
  font-family:{font};
  color:{text};
  -webkit-font-smoothing:antialiased;
}}
.card{{
  width:100%;
  padding:34px 38px 30px;
  position:relative;
  overflow:hidden;
}}
.card::before{{content:none;}}
.accent-bar{{
  width:100%;height:4px;
  background:linear-gradient(90deg,{accent} 0%,{accent2} 100%);
}}
.eyebrow{{
  font-size:13px;font-weight:700;letter-spacing:0.22em;
  text-transform:uppercase;color:{accent};
}}
.title{{
  font-size:30px;font-weight:800;line-height:1.25;
  color:{text};margin-top:10px;
}}
.footer{{
  margin-top:26px;padding-top:16px;
  border-top:1px solid {line};
  font-size:13px;color:{faint};
  display:flex;align-items:center;
}}
.footer .spacer{{flex:1;}}
"""


def _base_css(accent: str, accent2: str | None = None) -> str:
    return _BASE.format(
        bg=_BG,
        font=_FONT,
        text=_TEXT,
        line=_LINE,
        faint=_TEXT_FAINT,
        accent=accent,
        accent2=accent2 or accent,
    )


# ─────────────────────────────────────────────
# 1. 摘要卡片 summary_card
# ─────────────────────────────────────────────


def summary_card(
    title: str,
    points: Sequence[str],
    *,
    eyebrow: str = "",
    footer: str = "",
    accent: str = _GOLD,
) -> str:
    """要点摘要卡片：眉题 + 大标题 + 圆点要点列表 + 页脚。

    适合「总结今日内容」「归纳要点」「复盘」等场景。
    """
    items = "\n".join(
        f'<div class="pt"><div class="dot"></div><div class="pt-text">{_e(p)}</div></div>' for p in points
    )
    eyebrow_html = f'<div class="eyebrow">{_e(eyebrow)}</div>' if eyebrow else ""
    footer_html = f'<div class="footer">{_e(footer)}<div class="spacer"></div></div>' if footer else ""

    style = (
        _base_css(accent)
        + f"""
.pt{{display:flex;align-items:flex-start;margin-top:16px;}}
.dot{{
  width:8px;height:8px;border-radius:4px;background:{accent};
  margin-top:9px;margin-right:14px;flex:none;
}}
.pt-text{{font-size:17px;line-height:1.6;color:{_TEXT};}}
"""
    )
    body = f"""
<div class="accent-bar"></div>
<div class="card">
  {eyebrow_html}
  <div class="title">{_e(title)}</div>
  <div class="points">{items}</div>
  {footer_html}
</div>
"""
    return _page(style, body)


# ─────────────────────────────────────────────
# 2. 排行榜 ranking_card
# ─────────────────────────────────────────────


def ranking_card(
    title: str,
    rows: Sequence[tuple[str, str]],
    *,
    eyebrow: str = "",
    value_label: str = "",
    footer: str = "",
    accent: str = _CORAL,
) -> str:
    """排行榜卡片：序号徽章 + 名称 + 右对齐数值。

    ``rows`` 为 ``(名称, 数值文本)`` 序列，前三名使用金/银/铜奖牌色。
    适合伤害排行、积分榜、热度榜等。
    """
    rendered: list[str] = []
    for i, (name, value) in enumerate(rows):
        medal = _MEDALS[i] if i < len(_MEDALS) else _SURFACE_ALT
        badge_color = "#141d30" if i < len(_MEDALS) else _TEXT_DIM
        rendered.append(
            f'<div class="row">'
            f'<div class="badge" style="background:{medal};color:{badge_color}">{i + 1}</div>'
            f'<div class="name">{_e(name)}</div>'
            f'<div class="value">{_e(value)}</div>'
            f"</div>"
        )
    rows_html = "\n".join(rendered)
    eyebrow_html = f'<div class="eyebrow">{_e(eyebrow)}</div>' if eyebrow else ""
    label_html = (
        f'<div class="spacer"></div><div>{_e(value_label)}</div>' if value_label else '<div class="spacer"></div>'
    )
    footer_html = f'<div class="footer">{_e(footer)}{label_html}</div>' if (footer or value_label) else ""

    style = (
        _base_css(accent)
        + f"""
.row{{
  display:flex;align-items:center;
  background:{_SURFACE};border-radius:12px;
  padding:13px 18px;margin-top:12px;
}}
.badge{{
  width:32px;height:32px;border-radius:16px;flex:none;
  font-size:16px;font-weight:800;
  display:flex;align-items:center;justify-content:center;
}}
.name{{margin-left:16px;flex:1;font-size:17px;font-weight:600;color:{_TEXT};}}
.value{{margin-left:auto;font-size:15px;font-weight:600;color:{accent};}}
"""
    )
    body = f"""
<div class="accent-bar"></div>
<div class="card">
  {eyebrow_html}
  <div class="title">{_e(title)}</div>
  {rows_html}
  {footer_html}
</div>
"""
    return _page(style, body)


# ─────────────────────────────────────────────
# 3. 对比表 comparison_card
# ─────────────────────────────────────────────

# 只有无歧义的勾选/叉号符号才自动着色（✓ 绿 / ✕ 红）。
# 「是/否」「支持/不支持」这类词本身没有好坏含义（例如「需要浏览器：否」
# 反而是优势），不能按字面着色，因此不纳入自动着色范围。
#
# 注意：MiSans 不含 ✗ (U+2717) 与 ▸ 字形，渲染会变空白，因此统一把
# 这些变体归一化成 MiSans 支持的 ✕ (U+2715)。
_POSITIVE = {"✓", "✔", "√"}
_NEGATIVE = {"✕", "×", "x"}
# 渲染前做字形归一化，避免缺字空白（key 为 MiSans 缺字的符号）
_GLYPH_NORMALIZE = str.maketrans({"✗": "✕", "✘": "✕"})


def _norm_glyphs(value: str) -> str:
    """把 MiSans 缺字的符号替换成可渲染的等价符号。"""
    return value.translate(_GLYPH_NORMALIZE)


def _cell_style(value: str) -> str:
    v = value.strip()
    if v in _POSITIVE:
        return f"color:{_TEAL};font-weight:700;"
    if v.lower() in _NEGATIVE:
        return f"color:{_CORAL};font-weight:700;"
    return ""


def comparison_card(
    title: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    eyebrow: str = "",
    footer: str = "",
    accent: str = _BLUE,
) -> str:
    """方案/功能对比表：flex 模拟表格（引擎不支持 CSS table）。

    首列作为标签列稍宽；单元格内容为 ✓/✗/是/否 等时自动着色。
    """
    n = len(headers)
    header_cells = "".join(
        f'<div class="cell head{" first" if j == 0 else ""}">{_e(h)}</div>' for j, h in enumerate(headers)
    )
    rendered_rows: list[str] = []
    for i, row in enumerate(rows):
        cells = []
        for j in range(n):
            val = _norm_glyphs(row[j] if j < len(row) else "")
            extra = _cell_style(val)
            cls = f"cell{' first' if j == 0 else ''}"
            style_attr = f' style="{extra}"' if extra else ""
            cells.append(f'<div class="{cls}"{style_attr}>{_e(val)}</div>')
        alt = " alt" if i % 2 == 1 else ""
        rendered_rows.append(f'<div class="trow{alt}">{"".join(cells)}</div>')
    rows_html = "\n".join(rendered_rows)

    eyebrow_html = f'<div class="eyebrow">{_e(eyebrow)}</div>' if eyebrow else ""
    footer_html = f'<div class="footer">{_e(footer)}<div class="spacer"></div></div>' if footer else ""

    style = (
        _base_css(accent)
        + f"""
.table{{margin-top:18px;border-radius:12px;overflow:hidden;border:1px solid {_LINE};}}
.trow{{display:flex;background:{_SURFACE};}}
.trow.alt{{background:{_SURFACE_ALT};}}
.trow.headrow{{background:{accent};}}
.cell{{
  flex:1;padding:12px 16px;font-size:15px;line-height:1.5;color:{_TEXT};
  border-right:1px solid {_LINE};
}}
.cell:last-child{{border-right:none;}}
.cell.first{{flex:1.4;font-weight:600;}}
.cell.head{{font-weight:800;color:#101828;background:{accent};font-size:15px;}}
"""
    )
    body = f"""
<div class="accent-bar"></div>
<div class="card">
  {eyebrow_html}
  <div class="title">{_e(title)}</div>
  <div class="table">
    <div class="trow headrow">{header_cells}</div>
    {rows_html}
  </div>
  {footer_html}
</div>
"""
    return _page(style, body)


# ─────────────────────────────────────────────
# 4. 引用卡片 quote_card
# ─────────────────────────────────────────────


def quote_card(
    text: str,
    *,
    attribution: str = "",
    footer: str = "",
    accent: str = _GOLD,
) -> str:
    """金句/引用卡片：大号装饰引号 + 引文 + 署名。"""
    attribution_html = f'<div class="attrib">—— {_e(attribution)}</div>' if attribution else ""
    footer_html = f'<div class="footer">{_e(footer)}<div class="spacer"></div></div>' if footer else ""

    style = (
        _base_css(accent)
        + f"""
.qmark{{
  font-size:72px;font-weight:800;line-height:0.5;color:{accent};
  margin-top:14px;
}}
.quote{{
  font-size:23px;font-weight:600;line-height:1.65;color:{_TEXT};
  margin-top:26px;white-space:pre-wrap;
}}
.attrib{{margin-top:22px;font-size:15px;color:{_TEXT_DIM};}}
"""
    )
    body = f"""
<div class="accent-bar"></div>
<div class="card">
  <div class="qmark">&ldquo;</div>
  <div class="quote">{_e(text)}</div>
  {attribution_html}
  {footer_html}
</div>
"""
    return _page(style, body)


# ─────────────────────────────────────────────
# 5. 数据指标 metrics_card
# ─────────────────────────────────────────────


def metrics_card(
    title: str,
    metrics: Sequence[tuple[str, str]],
    *,
    eyebrow: str = "",
    deltas: Sequence[str] | None = None,
    footer: str = "",
    accent: str = _TEAL,
) -> str:
    """数据指标卡片：横排统计块，大号数字 + 标签 + 可选增减幅。

    ``metrics`` 为 ``(数值, 标签)`` 序列；``deltas`` 与之一一对应，
    以 ``+``/``-`` 开头的增减幅会自动着绿/红。
    """
    blocks: list[str] = []
    for i, (value, label) in enumerate(metrics):
        delta_html = ""
        if deltas and i < len(deltas) and deltas[i]:
            d = deltas[i]
            color = _TEAL if d.lstrip().startswith("+") else _CORAL
            delta_html = f'<div class="delta" style="color:{color}">{_e(d)}</div>'
        blocks.append(
            f'<div class="metric">'
            f'<div class="m-value">{_e(value)}</div>'
            f'<div class="m-label">{_e(label)}</div>'
            f"{delta_html}"
            f"</div>"
        )
    metrics_html = "\n".join(blocks)
    eyebrow_html = f'<div class="eyebrow">{_e(eyebrow)}</div>' if eyebrow else ""
    footer_html = f'<div class="footer">{_e(footer)}<div class="spacer"></div></div>' if footer else ""

    style = (
        _base_css(accent)
        + f"""
.metrics{{display:flex;gap:14px;margin-top:22px;}}
.metric{{
  flex:1;background:{_SURFACE};border-radius:14px;
  padding:20px 18px;border-top:3px solid {accent};
}}
.m-value{{font-size:30px;font-weight:800;color:{_TEXT};line-height:1.1;}}
.m-label{{font-size:13px;color:{_TEXT_DIM};margin-top:8px;}}
.delta{{font-size:13px;font-weight:700;margin-top:8px;}}
"""
    )
    body = f"""
<div class="accent-bar"></div>
<div class="card">
  {eyebrow_html}
  <div class="title">{_e(title)}</div>
  <div class="metrics">{metrics_html}</div>
  {footer_html}
</div>
"""
    return _page(style, body)


# ─────────────────────────────────────────────
# 6. 通知公告 notice_card
# ─────────────────────────────────────────────


def notice_card(
    title: str,
    body_text: str,
    *,
    tag: str = "",
    meta: str = "",
    kind: str = "info",
    footer: str = "",
) -> str:
    """通知公告卡片：左侧主色竖条 + 标签 + 标题 + 正文 + 元信息。

    ``kind`` 可选 ``info`` / ``success`` / ``warning`` / ``danger``，决定主色。
    """
    accents = {
        "info": _BLUE,
        "success": _TEAL,
        "warning": _GOLD,
        "danger": _CORAL,
    }
    accent = accents.get(kind, _BLUE)
    tag_html = f'<div class="tag" style="background:{accent};color:#101828">{_e(tag)}</div>' if tag else ""
    meta_html = f'<div class="meta">{_e(meta)}</div>' if meta else ""
    footer_html = f'<div class="footer">{_e(footer)}<div class="spacer"></div></div>' if footer else ""

    style = (
        _base_css(accent)
        + f"""
.notice{{display:flex;}}
.rail{{width:5px;border-radius:3px;background:{accent};flex:none;margin-right:22px;}}
.notice-body{{flex:1;}}
.tag{{
  display:inline-block;padding:4px 12px;border-radius:999px;
  font-size:12px;font-weight:800;letter-spacing:0.08em;
}}
.n-title{{font-size:24px;font-weight:800;color:{_TEXT};margin-top:14px;line-height:1.3;}}
.n-body{{
  font-size:16px;line-height:1.7;color:{_TEXT_DIM};
  margin-top:12px;white-space:pre-wrap;
}}
.meta{{margin-top:18px;font-size:13px;color:{_TEXT_FAINT};}}
"""
    )
    body = f"""
<div class="accent-bar"></div>
<div class="card">
  <div class="notice">
    <div class="rail"></div>
    <div class="notice-body">
      {tag_html}
      <div class="n-title">{_e(title)}</div>
      <div class="n-body">{_e(body_text)}</div>
      {meta_html}
    </div>
  </div>
  {footer_html}
</div>
"""
    return _page(style, body)


# ─────────────────────────────────────────────
# 7. 步骤教程 steps_card
# ─────────────────────────────────────────────


def steps_card(
    title: str,
    steps: Sequence[tuple[str, str]],
    *,
    eyebrow: str = "",
    footer: str = "",
    accent: str = _TEAL,
) -> str:
    """步骤教程卡片：编号圆点 + 竖向连接线 + 步骤标题与说明。

    ``steps`` 为 ``(步骤标题, 说明)`` 序列。
    """
    rendered: list[str] = []
    last = len(steps) - 1
    for i, (step_title, desc) in enumerate(steps):
        connector = "" if i == last else '<div class="connector"></div>'
        rendered.append(
            f'<div class="step">'
            f'<div class="step-rail">'
            f'<div class="step-badge">{i + 1}</div>'
            f"{connector}"
            f"</div>"
            f'<div class="step-content">'
            f'<div class="step-title">{_e(step_title)}</div>'
            f'<div class="step-desc">{_e(desc)}</div>'
            f"</div>"
            f"</div>"
        )
    steps_html = "\n".join(rendered)
    eyebrow_html = f'<div class="eyebrow">{_e(eyebrow)}</div>' if eyebrow else ""
    footer_html = f'<div class="footer">{_e(footer)}<div class="spacer"></div></div>' if footer else ""

    style = (
        _base_css(accent)
        + f"""
.step{{display:flex;margin-top:22px;}}
.step-rail{{display:flex;flex-direction:column;align-items:center;flex:none;}}
.step-badge{{
  width:32px;height:32px;border-radius:16px;background:{accent};
  color:#101828;font-size:16px;font-weight:800;
  display:flex;align-items:center;justify-content:center;
}}
.connector{{flex:1;width:2px;background:{accent};margin-top:8px;}}
.step-content{{margin-left:18px;padding-bottom:6px;flex:1;}}
.step-title{{font-size:18px;font-weight:700;color:{_TEXT};}}
.step-desc{{font-size:15px;line-height:1.6;color:{_TEXT_DIM};margin-top:5px;white-space:pre-wrap;}}
"""
    )
    body = f"""
<div class="accent-bar"></div>
<div class="card">
  {eyebrow_html}
  <div class="title">{_e(title)}</div>
  {steps_html}
  {footer_html}
</div>
"""
    return _page(style, body)


# ─────────────────────────────────────────────
# 8. 代码卡片 code_card
# ─────────────────────────────────────────────


def code_card(
    code: str,
    *,
    title: str = "",
    language: str = "",
    filename: str = "",
    footer: str = "",
    accent: str = _VIOLET,
) -> str:
    """代码卡片：终端窗口风格（三点窗控 + 文件名）+ 等宽代码块。"""
    lang_html = f'<div class="lang">{_e(language)}</div>' if language else ""
    fname_html = f'<div class="fname">{_e(filename)}</div>' if filename else ""
    title_html = f'<div class="title">{_e(title)}</div>' if title else ""
    footer_html = f'<div class="footer">{_e(footer)}<div class="spacer"></div></div>' if footer else ""

    style = (
        _base_css(accent)
        + f"""
.window{{
  margin-top:20px;border-radius:14px;overflow:hidden;
  border:1px solid {_LINE};background:#0d1420;
}}
.chrome{{
  display:flex;align-items:center;padding:12px 16px;
  background:{_SURFACE};border-bottom:1px solid {_LINE};
}}
.traffic{{display:flex;gap:7px;}}
.tdot{{width:11px;height:11px;border-radius:6px;}}
.fname{{margin-left:14px;font-size:13px;color:{_TEXT_DIM};font-family:{_MONO};}}
.lang{{margin-left:auto;font-size:12px;font-weight:700;color:{accent};letter-spacing:0.06em;}}
pre{{
  margin:0;padding:20px 22px;
  font-family:{_MONO};font-size:14px;line-height:1.65;
  color:#d6e2f2;white-space:pre-wrap;word-wrap:break-word;
}}
"""
    )
    body = f"""
<div class="accent-bar"></div>
<div class="card">
  {title_html}
  <div class="window">
    <div class="chrome">
      <div class="traffic">
        <div class="tdot" style="background:{_CORAL}"></div>
        <div class="tdot" style="background:{_GOLD}"></div>
        <div class="tdot" style="background:{_TEAL}"></div>
      </div>
      {fname_html}
      {lang_html}
    </div>
    <pre>{_e(code)}</pre>
  </div>
  {footer_html}
</div>
"""
    return _page(style, body)


# ─────────────────────────────────────────────
# 渲染封装：HTML → 图片字节
# ─────────────────────────────────────────────


async def _render(html: str, *, image_format: str = "png", jpeg_quality: int = 92) -> bytes:
    return await render_html_to_bytes(
        html,
        max_width=float(CARD_WIDTH),
        image_format=image_format,
        jpeg_quality=jpeg_quality,
    )


async def render_summary_card(*args, **kwargs) -> bytes:
    return await _render(summary_card(*args, **kwargs))


async def render_ranking_card(*args, **kwargs) -> bytes:
    return await _render(ranking_card(*args, **kwargs))


async def render_comparison_card(*args, **kwargs) -> bytes:
    return await _render(comparison_card(*args, **kwargs))


async def render_quote_card(*args, **kwargs) -> bytes:
    return await _render(quote_card(*args, **kwargs))


async def render_metrics_card(*args, **kwargs) -> bytes:
    return await _render(metrics_card(*args, **kwargs))


async def render_notice_card(*args, **kwargs) -> bytes:
    return await _render(notice_card(*args, **kwargs))


async def render_steps_card(*args, **kwargs) -> bytes:
    return await _render(steps_card(*args, **kwargs))


async def render_code_card(*args, **kwargs) -> bytes:
    return await _render(code_card(*args, **kwargs))
