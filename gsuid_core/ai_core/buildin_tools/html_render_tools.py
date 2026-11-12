"""HTML渲染工具模块

提供将 HTML / Markdown / 结构化卡片渲染为图片的能力，供 AI 调用。
主人格出站权限下可 bot 直发；能力代理仅登记 artifact / 回传 bytes，由主人格发送。
"""

import re
import json
import base64
import asyncio
import datetime
from typing import TYPE_CHECKING, Any, Tuple, Literal, Optional
from pathlib import Path

import httpx
from pydantic_ai import RunContext

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.html_render import im_templates, render_md_to_bytes, render_html_to_bytes
from gsuid_core.utils.resource_manager import RM
from gsuid_core.ai_core.buildin_tools.chart_svg import resolve_chart_placeholders
from gsuid_core.ai_core.buildin_tools.render_kit import PHOTO_LAYOUTS, page_layout, inject_render_kit

# 嵌图上限：防 agent 把数 MB 原图塞进 HTML 撑爆上下文
_EMBED_MAX_BYTES = 450_000
_EMBED_HTTP_TIMEOUT = 12.0
_EMBED_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 GsCore-HTMLEmbed/1.0"
# 单次 HTML 自动嵌图：位图资源与轻量矢量图标分开计数，防多图标挤掉后半段 icon:
_AUTO_EMBED_MAX_IMAGES = 40
_AUTO_EMBED_MAX_ICONS = 96
# 840 逻辑宽 × 2 倍像素比，满宽头图要 1680px 才不糊；嵌入图最长边仍是 1280
_AUTO_EMBED_MAX_SIDE = 1280
_AUTO_EMBED_CONCURRENCY = 4
_ICONIFY_CACHE_MAX = 512
_ICONIFY_SVG_CACHE: dict[str, bytes] = {}
_ICONIFY_RAW_CACHE: dict[str, tuple[int, int, int, int, str]] = {}
_ICONIFY_API_HOSTS = (
    "https://api.iconify.design",
    "https://api.simplesvg.com",
    "https://api.unisvg.com",
)
_ICON_NAME_FALLBACKS: dict[str, str] = {
    "pagoda": "temple-buddhist",
    "globe-outline": "earth",
    "call-split": "source-fork",
    "monitor-question": "monitor-eye",
    "mountain": "image-filter-hdr",
}
_ICON_SOURCE_RE = re.compile(
    r"^icon:(?P<prefix>[a-z0-9-]+)/(?P<name>[a-z0-9-]+)"
    r"(?:\?color=(?:#|%23)?(?P<color>[0-9a-f]{3}|[0-9a-f]{6}))?$",
    re.IGNORECASE,
)
# HTML <img src="...">（属性顺序任意）
_IMG_SRC_ATTR_RE = re.compile(
    r'(?P<pre><img\b[^>]*?\bsrc\s*=\s*)(?P<q>["\'])(?P<src>[^"\']+)(?P=q)',
    re.IGNORECASE | re.DOTALL,
)
# CSS url(...)：仅匹配需解析的前缀，避免动 data: / 相对路径 / 字体
_CSS_URL_RE = re.compile(
    r"url\(\s*(?P<q>['\"]?)(?P<src>(?:https?://|icon:|img_|res_)[^'\"\)\s]+?)(?P=q)\s*\)",
    re.IGNORECASE,
)
_UNRESOLVED_CHART_PLACEHOLDER_RE = re.compile(
    r"__(?:CHART|SVG|PLOT|GRAPH)[A-Z0-9_]*__"
    r"|\{\{\s*(?:chart|svg|plot|graph)[^}]*\}\}",
    re.IGNORECASE,
)
# 解析失败时的真正 RGBA(0,0,0,0) 1×1 全透明 PNG（旧常量误用半透明红像素会导致粉色方块）
_TRANSPARENT_1PX_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABpfZFQAAAAABJRU5ErkJggg=="
)
_TAKUMI_PRE_RESET_CSS = """
/* takumi-pre-reset */
pre{padding:14px 16px;border-radius:12px;background:rgba(128,128,128,0.12);
  border:1px solid rgba(128,128,128,0.22);color:inherit;
  font-family:"Mono",Consolas,Menlo,monospace;font-size:14.5px;line-height:1.58;white-space:pre-wrap;}
code{font-family:"Mono",Consolas,Menlo,monospace;}
"""

# Takumi 引擎基础 CSS，不强制 body 背景或内边距，避免覆盖自由排版主题。
_TAKUMI_ENGINE_HYGIENE_CSS = """
/* engine hygiene — not a visual theme */
img{max-width:100%;height:auto;}
/* chips & keys: block 2-char CJK vertical crush in flex rows.
   不要给 tag 加 border-box，Takumi shrink-to-fit 会把 padding 从字宽里扣掉。 */
.tag,.badge,.chip,.pill,.label,.kv-k,.kv-key{
  white-space:nowrap;
  word-break:keep-all;
  writing-mode:horizontal-tb;
  width:auto;
  flex-shrink:0;
}
.ico,.icon,.icon-box,.ch-ico,.sec-ico,.h2-ico{flex-shrink:0;}
/* brand/logo: single horizontal run, not stacked glyphs in a square */
.logo,.brand,.brand-mark,.icon-label{
  writing-mode:horizontal-tb;white-space:nowrap;word-break:keep-all;
  text-align:center;
}
"""

# IM 出图逻辑宽上限（CSS 像素）。再宽 QQ 气泡按宽缩小后正文会糊。
_LOGICAL_WIDTH_MAX = 1000
_LOGICAL_WIDTH_DEFAULT = 840


def _clamp_logical_width(max_width: int) -> int:
    try:
        w = int(max_width)
    except (TypeError, ValueError):
        return _LOGICAL_WIDTH_DEFAULT
    if w < 1:
        return _LOGICAL_WIDTH_DEFAULT
    return min(w, _LOGICAL_WIDTH_MAX)


# 同一 Kanban 任务内成功出图次数（防 render_agent 连调多次刷屏）
# key=task_id；进程内即可，任务结束无强清
_RENDER_EMITTED_TASKS: set[str] = set()
_RENDER_EMITTED_MAX = 256
# 透明像素兜底底色：仅当角点采不到不透明像素时使用（优先采样画面已有实色）。
# 这是管线兜底，不是强制 agent 只能用暗色主题。
_OPAQUE_FALLBACK_RGB = (15, 23, 42)  # #0f172a

_MD_CSS_PATH = str(Path(__file__).resolve().parent.parent.parent / "utils" / "html_render" / "markdown_dark.css")

_FOOTER_TEMPLATE = "\n\n---\n\n> AI 生成资料 · 数据可能滞后 · 仅供参考 · {ts}"

# 可选暗色设计系统壳（仅显式调用 ``_wrap_with_design_shell`` 时使用）。
# ``render_html_to_image`` 默认**不**套壳——agent 按内容自由写完整 HTML / 自带 <style>。
_HTML_SHELL = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{{margin:0;padding:0;}}
body{{
  width:800px;overflow:hidden;
  background:
    radial-gradient(ellipse 70% 50% at 15% 0%,rgba(91,157,217,0.08) 0%,transparent 55%),
    radial-gradient(ellipse 50% 40% at 85% 95%,rgba(62,201,167,0.06) 0%,transparent 50%),
    linear-gradient(175deg,#080d18 0%,#0d1424 50%,#0a1020 100%);
  font-family:"MiSans","PingFang SC","Microsoft YaHei",sans-serif;
  color:#edf2fa;line-height:1.5;
  padding:28px 28px 8px;
}}
h1{{font-size:26px;font-weight:700;color:#fff;margin:0 0 6px;line-height:1.25;}}
h2{{
  font-size:16px;font-weight:630;color:#e9eef7;margin:22px 0 12px;
}}
.meta{{font-size:12px;color:#6b7890;margin-bottom:16px;letter-spacing:0.02em;}}
.grid,.mgrid{{display:flex;gap:12px;margin:8px 0 4px;}}
.metric{{
  width:220px;background:linear-gradient(160deg,#1a2740,#131d30);
  border:1px solid rgba(91,157,217,0.18);border-radius:16px;
  padding:16px 14px;
  box-shadow:0 4px 18px rgba(0,0,0,0.32);
}}
.metric .lab,.lab{{font-size:11px;color:#6b7890;font-weight:700;letter-spacing:0.08em;}}
.metric .val,.val{{font-size:30px;font-weight:700;color:#fff;margin:6px 0 4px;line-height:1.05;}}
.metric .sub,.sub{{font-size:12px;color:#3ec9a7;font-weight:700;}}
.daygrid,.dgrid{{display:flex;gap:8px;margin-top:8px;}}
.day{{
  width:90px;background:linear-gradient(180deg,#1a2740,#131d30);
  border:1px solid rgba(91,157,217,0.12);border-radius:14px;padding:12px 4px;
  display:flex;flex-direction:column;align-items:center;
  box-shadow:0 3px 14px rgba(0,0,0,0.22);
}}
.day .d,.d{{font-size:12px;font-weight:700;color:#9aa7bd;}}
.day .ico,.ico{{
  width:36px;height:36px;border-radius:18px;margin-top:8px;
  display:flex;align-items:center;justify-content:center;
  font-size:14px;font-weight:700;color:#0a1220;
  background:linear-gradient(145deg,#5b9dd9,#2a5f9a);
  box-shadow:0 3px 10px rgba(0,0,0,0.35);
}}
.ico.storm{{background:linear-gradient(145deg,#9d8cff,#5b4bb5);}}
.ico.rain{{background:linear-gradient(145deg,#5b9dd9,#2a5f9a);}}
.ico.cloud{{background:linear-gradient(145deg,#8fa3bf,#4a5d75);}}
.ico.sun{{background:linear-gradient(145deg,#e8b45a,#b07a28);}}
.ico.hot{{background:linear-gradient(145deg,#ef7d6c,#b04538);}}
.day .w,.w{{font-size:11px;color:#9aa7bd;margin-top:8px;text-align:center;}}
.day .t .hi,.hi{{color:#fff;font-weight:630;}}
.day .t .lo,.lo{{color:#6b7890;font-weight:600;}}
.row{{
  display:flex;align-items:center;gap:14px;margin-top:10px;
  background:linear-gradient(135deg,#162035,#111a2c);
  border:1px solid rgba(91,157,217,0.1);border-radius:14px;
  padding:14px 16px;box-shadow:0 2px 12px rgba(0,0,0,0.22);
}}
.ico.sm{{
  width:40px;height:40px;border-radius:20px;flex:none;
  display:flex;align-items:center;justify-content:center;
  font-size:14px;font-weight:700;color:#0a1220;
  background:linear-gradient(145deg,#5b9dd9,#2a5f9a);
}}
.ico.sm.ok{{background:linear-gradient(145deg,#3ec9a7,#1f8a70);}}
.ico.sm.warn{{background:linear-gradient(145deg,#e8b45a,#b07a28);}}
.ico.sm.bad,.ico.sm.rain{{background:linear-gradient(145deg,#ef7d6c,#b04538);}}
.ico.sm.news{{background:linear-gradient(145deg,#5b9dd9,#2a5f9a);}}
.ico.sm.cool{{background:linear-gradient(145deg,#9d8cff,#5b4bb5);}}
.main{{flex:1;}}
.main .title,.title{{font-size:15px;font-weight:700;color:#e9eef7;line-height:1.35;}}
.main .desc,.desc{{font-size:12px;color:#8a9bb5;margin-top:4px;line-height:1.45;}}
.tag{{
  flex:none;padding:4px 10px;border-radius:999px;
  font-size:11px;font-weight:520;background:#5b9dd9;color:#0a1220;
}}
.tag.green{{background:#3ec9a7;}}
.tag.gold{{background:#e8b45a;}}
.tag.red{{background:#ef7d6c;}}
.bar{{
  height:4px;border-radius:2px;margin:8px 0 4px;
  background:linear-gradient(90deg,#5b9dd9,#3ec9a7,#e8b45a,#ef7d6c);
}}
.col{{display:flex;flex-direction:column;gap:10px;}}
.list{{margin-top:8px;}}
.item{{
  margin-top:10px;padding:12px 14px;border-radius:14px;
  background:linear-gradient(135deg,#162035,#111a2c);
  border:1px solid rgba(91,157,217,0.1);
}}
.pill{{
  display:inline-block;padding:3px 10px;border-radius:999px;
  font-size:11px;font-weight:520;background:#3ec9a7;color:#0a1220;
}}
.muted{{color:#8a9bb5;font-size:12px;}}
.big{{font-size:34px;font-weight:700;color:#fff;line-height:1.05;}}
.chart{{
  margin-top:10px;display:flex;flex-direction:column;gap:9px;
  padding:14px 16px;border-radius:14px;
  background:linear-gradient(135deg,#162035,#111a2c);
  border:1px solid rgba(91,157,217,0.1);
}}
.crow{{display:flex;align-items:center;gap:10px;}}
.clab{{width:84px;flex:none;font-size:12px;color:#9aa7bd;text-align:right;}}
.track{{flex:1;height:16px;border-radius:8px;background:rgba(255,255,255,0.06);overflow:hidden;}}
.fill{{height:16px;border-radius:8px;background:linear-gradient(90deg,#b04538,#ef7d6c);}}
.fill.up{{background:linear-gradient(90deg,#1f8a70,#3ec9a7);}}
.cval{{width:58px;flex:none;font-size:12px;font-weight:630;color:#fca5a5;}}
.cval.up{{color:#3ec9a7;}}
</style></head>
<body>
{body}
<div style="margin:20px 4px 16px;padding-top:12px;border-top:1px solid rgba(91,157,217,0.08);
  font-size:10px;color:#3d5068;letter-spacing:0.03em;">
  AI 生成资料 · 数据可能滞后 · 仅供参考 · {ts}</div>
</body></html>"""

_RENDER_OK_SENT = "图片已发送（{kb}KB）。台词只留一两句引导，禁止复述数据。"
_RENDER_OK_HANDLE = (
    "图片已生成（{kb}KB）{handle_part}。"
    "禁止对用户会话直发；主人格用 send_message_by_ai(image_id=句柄) 发送，台词一两句。"
)

_CARD_TYPES = frozenset({"weather", "news", "metrics", "ranking", "summary", "steps", "comparison", "board"})


def _footer() -> str:
    return _FOOTER_TEMPLATE.format(ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))


def _wrap_with_design_shell(html_content: str) -> str:
    """可选：套暗色设计系统壳（离线 demo / 测试用）。``render_html_to_image`` 默认不调用。"""
    html = html_content.strip()
    lower = html[:200].lower()
    if lower.startswith("<!doctype") or lower.startswith("<html"):
        return html
    return _HTML_SHELL.format(
        body=html,
        ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


# deprecated: 仅旧测试别名，新代码请用 _wrap_with_design_shell
_wrap_html_if_needed = _wrap_with_design_shell


def _inject_style(html: str, css: str) -> str:
    """把 CSS 注入到已有 <style> / <head>，否则包一层最小文档。"""
    css_block = f"<style>\n{css}\n</style>"
    lower = html.lower()
    style_close = lower.rfind("</style>")
    if style_close != -1:
        return html[:style_close] + "\n" + css + "\n" + html[style_close:]
    head_close = lower.find("</head>")
    if head_close != -1:
        return html[:head_close] + css_block + "\n" + html[head_close:]
    # 片段：前置 style，由 html_to_pic 抽取
    return f"{css_block}\n{html}"


def _rewrite_html_tables(html: str) -> str:
    """原生 <table> → flex 行（Takumi 无 CSS table；见 ``table_rewrite``）。

    自由 HTML 强制本地改写（保留 td/th class/style）；注入 CSS 按页面主题选择，
    默认 layout-only，避免深色页套浅色表导致看不清。
    """
    if "<table" not in html.lower():
        return html
    from gsuid_core.utils.html_render.table_rewrite import (
        md_table_flex_css,
        rewrite_tables_for_takumi,
    )

    # rowspan/colspan 仅近似：不 raise，留下排障信号
    if re.search(r"\b(?:rowspan|colspan)\s*=", html, flags=re.I):
        logger.warning(t("log.ai.buildintools_html_table_rowspan_colspan"))

    # prefer_local：保留 .up/.down 等 agent 写在 td 上的 class
    rewritten = rewrite_tables_for_takumi(html, prefer_local=True)
    if "md-table" not in rewritten:
        return rewritten
    compact = rewritten.replace(" ", "").replace("\n", "")
    if ".md-table{" in compact or "md-table{display" in compact:
        return rewritten
    # 自由 HTML 一律 layout-only：不写死字色/表底，避免盖掉 body 与 .up/.down
    return _inject_style(rewritten, md_table_flex_css(theme="layout"))


_GS_KV_PAIR_RE = re.compile(
    r'(<(?:div|span)\b[^>]*\bclass=["\'][^"\']*\bgs-k\b[^"\']*["\'][^>]*>'
    r"(?:(?!</?(?:div|span)\b).)*</(?:div|span)>\s*"
    r'<(?:div|span)\b[^>]*\bclass=["\'][^"\']*\bgs-v\b[^"\']*["\'][^>]*>'
    r"(?:(?!</?(?:div|span)\b).)*</(?:div|span)>)",
    re.IGNORECASE | re.DOTALL,
)
_GS_KV_MULTI_RE = re.compile(
    r'<div\s+class=(["\'])gs-kv\1>\s*'
    r"((?:<(?:div|span)\b[^>]*\bclass=[\"'][^\"']*\bgs-k\b[^\"']*[\"'][^>]*>"
    r"(?:(?!</?(?:div|span)\b).)*</(?:div|span)>\s*"
    r"<(?:div|span)\b[^>]*\bclass=[\"'][^\"']*\bgs-v\b[^\"']*[\"'][^>]*>"
    r"(?:(?!</?(?:div|span)\b).)*</(?:div|span)>\s*){2,})</div>",
    re.IGNORECASE | re.DOTALL,
)
_GS_TIP_BLOCK_RE = re.compile(
    r'<div\s+class=(["\'])gs-tip\1>((?:(?!</div>\s*</div>).)*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_LEAF_DIV_RE = re.compile(
    r"(<div\b[^>]*>)((?:(?!</?div\b).)+)(</div>)",
    re.IGNORECASE | re.DOTALL,
)
_BLOCK_OR_MEDIA_TAG_RE = re.compile(
    r"<(?:p|section|article|header|footer|ul|ol|li|table|tr|td|th|svg|img|pre|blockquote|hr|h[1-6])\b",
    re.IGNORECASE,
)
_INLINE_TYPO_TAG_RE = re.compile(
    r"<(?:br|b|strong|em|i|code|mark|small|sub|sup|u|s|span)\b",
    re.IGNORECASE,
)
_EMPTY_SPAN_RE = re.compile(r"<span\b[^>]*>\s*</span>", re.IGNORECASE)
_INNER_PAIRED_INLINE_RE = re.compile(
    r"<(b|strong|em|i|code|mark|small|sub|sup|u|s|span|a)\b[^>]*>"
    r"(?:(?!</?\1\b).)*</\1>",
    re.IGNORECASE | re.DOTALL,
)
_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style>)", re.IGNORECASE | re.DOTALL)
_CSS_RULE_RE = re.compile(r"([^{}@]+)\{([^{}]*)\}")
_DARK_HEX_BG_RE = re.compile(
    r"(?:^|;|\s)background(?:-color)?\s*:\s*#(?:[01][0-9a-f]{5}|2[0-6][0-9a-f]{4}|[01][0-9a-f]{2})\b",
    re.IGNORECASE,
)
_CSS_COLOR_PROP_RE = re.compile(r"(?:^|;|\s)color\s*:", re.IGNORECASE)


def _heal_dark_surface_contrast(html: str) -> str:
    """深底容器（#00..#26）漏写 color 时自动补亮色字，防浅色页内嵌暗色代码卡黑字撞黑底。"""

    def _patch_rule(rm: re.Match[str]) -> str:
        sel, body = rm.group(1), rm.group(2)
        if "::" in sel or _CSS_COLOR_PROP_RE.search(body) or not _DARK_HEX_BG_RE.search(body):
            return rm.group(0)
        sep = "" if body.rstrip().endswith(";") or not body.strip() else ";"
        return f"{sel}{{{body}{sep}color:#e2e8f0}}"

    def _patch_style(sm: re.Match[str]) -> str:
        return f"{sm.group(1)}{_CSS_RULE_RE.sub(_patch_rule, sm.group(2))}{sm.group(3)}"

    return _STYLE_BLOCK_RE.sub(_patch_style, html)


def _heal_flex_phrasing_leaves(html: str) -> str:
    """自愈 flex 叶子容器内的行内混排（防 <br> 横向拆列及 <b>/<span> 纵向断行，保留多列 <span> 表格行）。"""

    def _wrap_leaf(m: re.Match[str]) -> str:
        open_tag, inner, close_tag = m.group(1), m.group(2), m.group(3)
        if not _INLINE_TYPO_TAG_RE.search(inner):
            return m.group(0)
        if _BLOCK_OR_MEDIA_TAG_RE.search(inner) or _EMPTY_SPAN_RE.search(inner):
            return m.group(0)
        stripped = inner
        for _ in range(8):
            nxt = _INNER_PAIRED_INLINE_RE.sub("", stripped)
            if nxt == stripped:
                break
            stripped = nxt
        has_top_br = bool(re.search(r"<br\b", stripped, flags=re.I))
        bare = re.sub(r"<[^>]+>", "", stripped).strip()
        if not has_top_br and not bare:
            return m.group(0)
        return f'{open_tag}<div style="min-width:0">{inner}</div>{close_tag}'

    return _LEAF_DIV_RE.sub(_wrap_leaf, html)


_DIV_TOKEN_RE = re.compile(r"<div\b[^>]*>|</div>", re.IGNORECASE)
_TWO_COL_CLS_TOKENS = frozenset({"cols", "cols-2", "col-2", "grid-2", "two-col", "two-cols", "gs-grid", "split-2"})
_TR_CLS_RE = re.compile(r'\bclass\s*=\s*["\'][^"\']*\b(?:tr|trow|table-row)\b', re.IGNORECASE)
_TD_CELL_RE = re.compile(r'<(?:div|span)\b[^>]*\bclass=["\'][^"\']*\b(?:td|cell)\b', re.IGNORECASE)
_TR_HTML_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)


def _contains_wide_table(fragment: str) -> bool:
    for tr_m in _TR_HTML_RE.finditer(fragment):
        if len(re.findall(r"<t[dh]\b", tr_m.group(1), flags=re.I)) >= 5:
            return True
    stack: list[tuple[int, str]] = []
    for dm in _DIV_TOKEN_RE.finditer(fragment):
        tok = dm.group(0)
        if tok.lower().startswith("</div"):
            if not stack:
                continue
            inner_start, open_tok = stack.pop()
            if _TR_CLS_RE.search(open_tok) and len(_TD_CELL_RE.findall(fragment[inner_start : dm.start()])) >= 5:
                return True
        else:
            stack.append((dm.end(), tok))
    return False


def _is_two_col_open_tag(open_tag: str) -> bool:
    cls_m = re.search(r'\bclass\s*=\s*["\']([^"\']+)["\']', open_tag, flags=re.I)
    if cls_m:
        tokens = set(cls_m.group(1).lower().split())
        if "cols-3" not in tokens and (tokens & _TWO_COL_CLS_TOKENS):
            return True
    sty_m = re.search(r'\bstyle\s*=\s*["\']([^"\']+)["\']', open_tag, flags=re.I)
    if sty_m:
        sty = sty_m.group(1).lower()
        if "1fr 1fr" in sty or "repeat(2," in sty.replace(" ", ""):
            return True
    return False


def _heal_wide_tables_in_two_col(html: str) -> str:
    """当双列并排容器内塞入 ≥5 列的宽表时自动改为纵向全宽堆叠，防数字竖向挤碎。"""
    stack: list[tuple[int, int, str]] = []
    patches: list[tuple[int, int, str]] = []
    for m in _DIV_TOKEN_RE.finditer(html):
        tok = m.group(0)
        if tok.lower().startswith("</div"):
            if not stack:
                continue
            start_idx, end_open, open_tag = stack.pop()
            if _is_two_col_open_tag(open_tag) and _contains_wide_table(html[end_open : m.start()]):
                if re.search(r'\bstyle\s*=\s*["\']', open_tag, flags=re.I):
                    new_tag = re.sub(
                        r'(\bstyle\s*=\s*["\'])([^"\']*)(["\'])',
                        r"\1\2;flex-direction:column;grid-template-columns:1fr\3",
                        open_tag,
                        count=1,
                        flags=re.I,
                    )
                else:
                    new_tag = open_tag[:-1] + ' style="flex-direction:column;grid-template-columns:1fr">'
                patches.append((start_idx, end_open, new_tag))
        else:
            stack.append((m.start(), m.end(), tok))
    if not patches:
        return html
    out = html
    for s_idx, e_idx, replacement in sorted(patches, key=lambda x: x[0], reverse=True):
        out = out[:s_idx] + replacement + out[e_idx:]
    return out


def _heal_common_kit_traps(html: str) -> str:
    """自愈高频排版误用：多对键值挤单行 .gs-kv、无图标多块 .gs-tip、双栏塞宽表、深底漏字色、flex 叶子行内混排。"""
    html = _heal_dark_surface_contrast(html)
    html = _heal_wide_tables_in_two_col(html)
    if "gs-kv" in html:

        def _split_kv(m: re.Match[str]) -> str:
            pairs = _GS_KV_PAIR_RE.findall(m.group(2))
            if len(pairs) < 2:
                return m.group(0)
            rows = "".join(f'<div class="gs-kv">{p}</div>' for p in pairs)
            return f'<div class="gs-kv-list">{rows}</div>'

        html = _GS_KV_MULTI_RE.sub(_split_kv, html)

    if "gs-tip" in html:

        def _stack_tip(m: re.Match[str]) -> str:
            inner = m.group(2)
            if "gs-ico" in inner or "<img" in inner.lower():
                return m.group(0)
            if "gs-card-title" in inner or len(re.findall(r"<div\b", inner, flags=re.I)) >= 2:
                return f'<div class="gs-tip stack">{inner}</div>'
            return m.group(0)

        html = _GS_TIP_BLOCK_RE.sub(_stack_tip, html)

    return _heal_flex_phrasing_leaves(html)


def _prepend_style(html: str, css: str) -> str:
    """把低优先级默认样式前置到首个 <style> 之前，让 agent 自写规则优先胜出。"""
    block = f"<style>\n{css}\n</style>\n"
    lower = html.lower()
    head = re.search(r"<head\b[^>]*>", lower)
    if head is not None:
        return html[: head.end()] + "\n" + block + html[head.end() :]
    first_style = lower.find("<style")
    if first_style != -1:
        return html[:first_style] + block + html[first_style:]
    return block + html


def _prepare_free_html(html_content: str) -> str:
    """自由 HTML 预处理：展开图表句柄；自愈误用；按需前置组件库；table/SVG 改写；卫生 CSS。"""
    html = resolve_chart_placeholders(html_content.strip())
    if not html:
        return html
    html = _heal_common_kit_traps(html)
    if "<pre" in html.lower() and "takumi-pre-reset" not in html:
        html = _prepend_style(html, _TAKUMI_PRE_RESET_CSS)
    html = inject_render_kit(html)
    html = _rewrite_html_tables(html)
    from gsuid_core.utils.html_render.svg_chart_rewrite import rewrite_svg_charts_for_takumi

    html = rewrite_svg_charts_for_takumi(html)
    # 已注入过则跳过（避免重复）
    if "engine hygiene" not in html:
        html = _inject_style(html, _TAKUMI_ENGINE_HYGIENE_CSS)
    return html


def _ensure_opaque_image_bytes(
    data: bytes,
    *,
    fallback_rgb: tuple[int, int, int] = _OPAQUE_FALLBACK_RGB,
) -> bytes:
    """把带 alpha 的 PNG 合成到实色底，避免 IM 客户端显示透明/棋盘格。

    已全不透明则原样返回。合成后输出 RGB PNG。
    """
    if not data or len(data) < 24:
        return data

    from io import BytesIO

    from PIL import Image, UnidentifiedImageError

    # 仅兜底解码失败（损坏字节），不吞类型错误
    try:
        im = Image.open(BytesIO(data))
        im.load()
    except (OSError, UnidentifiedImageError, ValueError) as e:
        logger.debug(t("log.htmlrender.opaque_flatten_skip", e=e))
        return data

    # RGB/L 无 alpha；GIF 多为动图，不压平
    if im.mode in ("RGB", "L") or im.format == "GIF":
        return data

    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if not has_alpha:
        return data

    rgba = im.convert("RGBA")
    alpha = rgba.getchannel("A")
    mn, mx = alpha.getextrema()
    if mn == 255 and mx == 255:
        return data

    # 角点/中心已近似不透明时采样底色，贴近卡片真实主题
    w, h = rgba.size
    samples: list[tuple[int, int, int]] = []
    if w > 0 and h > 0:
        for xy in (
            (0, 0),
            (w - 1, 0),
            (0, h - 1),
            (w - 1, h - 1),
            (w // 2, h // 2),
        ):
            px = rgba.getpixel(xy)
            if isinstance(px, tuple) and len(px) >= 4 and int(px[3]) >= 250:
                samples.append((int(px[0]), int(px[1]), int(px[2])))
    if samples:
        n = len(samples)
        bg = (
            sum(c[0] for c in samples) // n,
            sum(c[1] for c in samples) // n,
            sum(c[2] for c in samples) // n,
        )
    else:
        bg = fallback_rgb
    base = Image.new("RGBA", rgba.size, (bg[0], bg[1], bg[2], 255))
    out = Image.alpha_composite(base, rgba).convert("RGB")
    buf = BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _render_emit_key(ctx: RunContext[ToolContext], task_id: str) -> str:
    # 群会话 id 人人共享，不能当去重键；无 task 时用本轮 turn_id。
    if task_id:
        return task_id
    extra = ctx.deps.extra
    turn = extra["turn_id"] if "turn_id" in extra else ""
    if isinstance(turn, str) and turn:
        return f"turn:{turn}"
    return ""


def _mark_render_emitted(task_id: str) -> None:
    if not task_id:
        return
    if len(_RENDER_EMITTED_TASKS) >= _RENDER_EMITTED_MAX:
        # 半量淘汰，防 set 无限涨
        for i, k in enumerate(list(_RENDER_EMITTED_TASKS)):
            if i % 2 == 0:
                _RENDER_EMITTED_TASKS.discard(k)
    _RENDER_EMITTED_TASKS.add(task_id)


async def _try_send_image(ctx: RunContext[ToolContext], image_bytes: bytes) -> bool:
    """仅主人格出站权限下尝试 bot 直发。能力代理永远 False。"""
    if not ctx.deps.allow_user_outbound:
        return False
    bot = ctx.deps.bot
    if bot is None:
        return False
    from gsuid_core.ai_core.agent_run.support import turn_reply_metadata

    try:
        await bot.send(
            MessageSegment.image(image_bytes),
            extra_metadata=turn_reply_metadata(ctx.deps.ev),
        )
        return True
    except Exception as e:
        logger.debug(t("log.htmlrender.auto_send_fallback", e=e))
        return False


_RENDER_SUMMARY_CHARS = 512


def clip_render_summary(goal: str, *, default: str = "render output") -> str:
    """图片摘要给 read_handle。默认「render output」读不到图上写了什么。"""
    text = goal.strip()
    if not text:
        return default
    if len(text) <= _RENDER_SUMMARY_CHARS:
        return text
    return text[: _RENDER_SUMMARY_CHARS - 1] + "…"


async def _summary_for_render(default: str) -> str:
    from gsuid_core.ai_core.planning.models import AIAgentTask, AIAgentArtifact
    from gsuid_core.ai_core.planning.runtime import get_plan_context
    from gsuid_core.ai_core.planning.kanban_tools import extract_res_ids

    plan = get_plan_context()
    if plan is None or not plan.task_id:
        return default
    task = await AIAgentTask.get_by_id(plan.task_id)
    if task is None:
        return default

    # 优先抽取上游依赖产物摘要，保证可视化产物具备数据依据
    res_ids: list[str] = []
    if isinstance(task.input_artifact_ids, list):
        for rid in task.input_artifact_ids:
            if isinstance(rid, str) and rid.startswith("res_") and rid not in res_ids:
                res_ids.append(rid)
    for rid in extract_res_ids(task.goal):
        if rid not in res_ids:
            res_ids.append(rid)

    source_summaries: list[str] = []
    for rid in res_ids[:3]:
        uart = await AIAgentArtifact.get_by_id(rid)
        if uart and uart.summary and uart.summary.strip():
            source_summaries.append(uart.summary.strip())

    if source_summaries:
        combined = "；".join(source_summaries)
        display = task.display_name.strip() if task.display_name else "可视化图表"
        return clip_render_summary(f"{display}（依据：{combined}）", default=default)

    return clip_render_summary(task.goal, default=default)


async def _register_image_artifact(image_bytes: bytes, *, summary: str = "render output") -> str:
    """Kanban 上下文：落盘 PNG 并 artifact_put；成功返回 res_ 句柄，否则空串。"""
    from gsuid_core.ai_core.planning.runtime import get_plan_context
    from gsuid_core.ai_core.planning.workspace import put_artifact

    if summary == "render output":
        summary = await _summary_for_render(summary)
    plan = get_plan_context()
    if plan is None or not plan.root_task_id:
        return ""
    workspace = plan.artifact_workspace
    if workspace is None:
        return ""
    workspace.mkdir(parents=True, exist_ok=True)
    fname = f"render_{plan.task_id[:8]}.png"
    path = workspace / fname
    path.write_bytes(image_bytes)
    art = await put_artifact(
        summary=summary[:512],
        mime="image/png",
        artifact_kind="output",
        plan_ctx=plan,
        file_path=path,
    )
    if art is None:
        return ""
    from gsuid_core.ai_core.planning.models import AIAgentTask

    await AIAgentTask.update_data_by_data(
        select_data={"id": plan.task_id},
        update_data={"output_artifact_id": art.id},
    )
    return art.id


async def _finish_image(ctx: RunContext[ToolContext], image_bytes: bytes) -> str | bytes:
    """不透明化 → 主人格可直发；能力代理只登记 artifact / 回传 bytes。

    同一 Kanban 任务默认只成功产出一张（防多段刷屏）。
    """
    from gsuid_core.ai_core.planning.runtime import get_plan_context

    image_bytes = _ensure_opaque_image_bytes(image_bytes)

    plan = get_plan_context()
    task_id = plan.task_id if plan is not None else ""
    emit_key = _render_emit_key(ctx, task_id)

    if emit_key and emit_key in _RENDER_EMITTED_TASKS:
        return (
            "⚠️ 本任务已成功出过图，本次未再产出。"
            "请把全部区块合并进**一张**完整 HTML，只调用一次 render_html_to_image；"
            "不要按章节拆成多次渲染。用户未明确要求多页时禁止连渲多张。"
        )

    # 能力代理 / 子 Agent：禁止直发；优先落盘 image artifact 供主人格发送
    if not ctx.deps.allow_user_outbound:
        handle = await _register_image_artifact(image_bytes)
        if handle:
            _mark_render_emitted(emit_key)
            return _RENDER_OK_HANDLE.format(
                kb=len(image_bytes) // 1024,
                handle_part=f"，句柄 `{handle}`",
            )
        # 无 plan 上下文：回传 bytes，由 agent loop 注册 img_ 资源
        return image_bytes

    if await _try_send_image(ctx, image_bytes):
        _mark_render_emitted(emit_key)
        # 主人格直发时仍尽量登记 artifact，便于追溯
        await _register_image_artifact(image_bytes)
        return _RENDER_OK_SENT.format(kb=len(image_bytes) // 1024)
    handle = await _register_image_artifact(image_bytes)
    if handle:
        # 直发失败但已有句柄，仍算本轮已产出。
        _mark_render_emitted(emit_key)
        return _RENDER_OK_HANDLE.format(
            kb=len(image_bytes) // 1024,
            handle_part=f"，句柄 `{handle}`",
        )
    return image_bytes


def _parse_payload(payload: str) -> dict[str, Any]:
    raw = (payload or "").strip()
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("payload 必须是 JSON 对象")
    return data


def _as_str_list(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(x) for x in items]


def _build_card_html(card_type: str, title: str, meta: str, payload: dict[str, Any]) -> str:
    footer = str(payload["footer"]) if "footer" in payload else "数据可能滞后 · 仅供参考"
    if card_type == "weather":
        metrics_raw = payload["metrics"] if "metrics" in payload else []
        days_raw = payload["days"] if "days" in payload else []
        tips_raw = payload["tips"] if "tips" in payload else []
        metrics: list[tuple[str, str, str]] = []
        if isinstance(metrics_raw, list):
            for m in metrics_raw:
                if isinstance(m, dict):
                    metrics.append(
                        (
                            str(m["label"] if "label" in m else ""),
                            str(m["value"] if "value" in m else ""),
                            str(m["sub"] if "sub" in m else ""),
                        )
                    )
                elif isinstance(m, (list, tuple)) and len(m) >= 2:
                    metrics.append((str(m[0]), str(m[1]), str(m[2]) if len(m) > 2 else ""))
        days: list[tuple[str, str, str, str, str]] = []
        if isinstance(days_raw, list):
            for d in days_raw:
                if isinstance(d, dict):
                    days.append(
                        (
                            str(d["day"] if "day" in d else ""),
                            str(d["icon"] if "icon" in d else "云"),
                            str(d["weather"] if "weather" in d else ""),
                            str(d["high"] if "high" in d else ""),
                            str(d["low"] if "low" in d else ""),
                        )
                    )
                elif isinstance(d, (list, tuple)) and len(d) >= 4:
                    days.append(
                        (
                            str(d[0]),
                            str(d[1]),
                            str(d[2]),
                            str(d[3]),
                            str(d[4]) if len(d) > 4 else "",
                        )
                    )
        tips: list[tuple[str, str, str]] = []
        if isinstance(tips_raw, list):
            for tip in tips_raw:
                if isinstance(tip, dict):
                    tips.append(
                        (
                            str(tip["icon"] if "icon" in tip else "防"),
                            str(tip["title"] if "title" in tip else ""),
                            str(tip["desc"] if "desc" in tip else ""),
                        )
                    )
                elif isinstance(tip, (list, tuple)) and len(tip) >= 2:
                    tips.append((str(tip[0]), str(tip[1]), str(tip[2]) if len(tip) > 2 else ""))
        return im_templates.weather_card(title, meta=meta, metrics=metrics, days=days, tips=tips, footer=footer)

    if card_type == "news":
        items_raw = payload["items"] if "items" in payload else []
        items: list[tuple[str, str, str, str]] = []
        if isinstance(items_raw, list):
            for it in items_raw:
                if isinstance(it, dict):
                    items.append(
                        (
                            str(it["icon"] if "icon" in it else "要"),
                            str(it["title"] if "title" in it else ""),
                            str(it["desc"] if "desc" in it else ""),
                            str(it["tag"] if "tag" in it else ""),
                        )
                    )
                elif isinstance(it, (list, tuple)) and len(it) >= 2:
                    items.append(
                        (
                            str(it[0]),
                            str(it[1]),
                            str(it[2]) if len(it) > 2 else "",
                            str(it[3]) if len(it) > 3 else "",
                        )
                    )
        return im_templates.news_card(title, items, meta=meta, footer=footer)

    if card_type == "metrics":
        metrics_raw = payload["metrics"] if "metrics" in payload else []
        pairs: list[tuple[str, str]] = []
        deltas: list[str] = []
        if isinstance(metrics_raw, list):
            for m in metrics_raw:
                if isinstance(m, dict):
                    pairs.append(
                        (
                            str(m["value"] if "value" in m else ""),
                            str(m["label"] if "label" in m else ""),
                        )
                    )
                    deltas.append(str(m["delta"] if "delta" in m else m["sub"] if "sub" in m else ""))
                elif isinstance(m, (list, tuple)) and len(m) >= 2:
                    # 允许 [label, value, delta] 或 [value, label]
                    if len(m) >= 3:
                        pairs.append((str(m[1]), str(m[0])))
                        deltas.append(str(m[2]))
                    else:
                        pairs.append((str(m[0]), str(m[1])))
                        deltas.append("")
        return im_templates.metrics_card(title, pairs, eyebrow=meta, deltas=deltas or None, footer=footer)

    if card_type == "ranking":
        rows_raw = payload["rows"] if "rows" in payload else []
        rows: list[tuple[str, str]] = []
        if isinstance(rows_raw, list):
            for r in rows_raw:
                if isinstance(r, dict):
                    rows.append(
                        (
                            str(r["name"] if "name" in r else ""),
                            str(r["value"] if "value" in r else ""),
                        )
                    )
                elif isinstance(r, (list, tuple)) and len(r) >= 2:
                    rows.append((str(r[0]), str(r[1])))
        return im_templates.ranking_card(title, rows, eyebrow=meta, footer=footer)

    if card_type == "summary":
        points = _as_str_list(payload["points"] if "points" in payload else [])
        return im_templates.summary_card(title, points, eyebrow=meta, footer=footer)

    if card_type == "steps":
        steps_raw = payload["steps"] if "steps" in payload else []
        steps: list[tuple[str, str]] = []
        if isinstance(steps_raw, list):
            for s in steps_raw:
                if isinstance(s, dict):
                    steps.append(
                        (
                            str(s["title"] if "title" in s else ""),
                            str(s["desc"] if "desc" in s else ""),
                        )
                    )
                elif isinstance(s, (list, tuple)) and len(s) >= 1:
                    steps.append((str(s[0]), str(s[1]) if len(s) > 1 else ""))
        return im_templates.steps_card(title, steps, eyebrow=meta, footer=footer)

    if card_type == "comparison":
        headers = _as_str_list(payload["headers"] if "headers" in payload else [])
        rows_raw = payload["rows"] if "rows" in payload else []
        rows2: list[list[str]] = []
        if isinstance(rows_raw, list):
            for r in rows_raw:
                if isinstance(r, (list, tuple)):
                    rows2.append([str(x) for x in r])
        return im_templates.comparison_card(title, headers, rows2, eyebrow=meta, footer=footer)

    if card_type == "board":
        metrics_raw = payload["metrics"] if "metrics" in payload else []
        sections_raw = payload["sections"] if "sections" in payload else []
        metrics_b: list[tuple[str, str, str]] = []
        if isinstance(metrics_raw, list):
            for m in metrics_raw:
                if isinstance(m, dict):
                    metrics_b.append(
                        (
                            str(m["label"] if "label" in m else ""),
                            str(m["value"] if "value" in m else ""),
                            str(m["sub"] if "sub" in m else m["delta"] if "delta" in m else ""),
                        )
                    )
                elif isinstance(m, (list, tuple)) and len(m) >= 2:
                    metrics_b.append((str(m[0]), str(m[1]), str(m[2]) if len(m) > 2 else ""))
        sections: list[tuple[str, list[tuple[str, str, str]]]] = []
        if isinstance(sections_raw, list):
            for sec in sections_raw:
                if not isinstance(sec, dict):
                    continue
                sec_title = str(sec["title"] if "title" in sec else "")
                rows_s: list[tuple[str, str, str]] = []
                rows_raw = sec["rows"] if "rows" in sec else []
                if isinstance(rows_raw, list):
                    for r in rows_raw:
                        if isinstance(r, dict):
                            rows_s.append(
                                (
                                    str(r["left"] if "left" in r else r["name"] if "name" in r else ""),
                                    str(r["mid"] if "mid" in r else r["desc"] if "desc" in r else ""),
                                    str(r["right"] if "right" in r else r["value"] if "value" in r else ""),
                                )
                            )
                        elif isinstance(r, (list, tuple)) and len(r) >= 2:
                            rows_s.append((str(r[0]), str(r[1]), str(r[2]) if len(r) > 2 else ""))
                sections.append((sec_title, rows_s))
        return im_templates.board_card(title, meta=meta, metrics=metrics_b, sections=sections, footer=footer)

    raise ValueError(f"未知 card_type: {card_type}，可选: {', '.join(sorted(_CARD_TYPES))}")


@ai_tools(category="media", capability_domain="资料出图", code_callable=False)
async def render_card(
    ctx: RunContext[ToolContext],
    card_type: Literal[
        "weather",
        "news",
        "metrics",
        "ranking",
        "summary",
        "steps",
        "comparison",
        "board",
    ],
    title: str,
    payload: str,
    meta: str = "",
) -> str | bytes:
    """将结构化 JSON 渲染为信息图并自动发送。**快捷次选**（固定布局）。

    多数据点出图**首选** ``render_html_to_image``（经 ``render_agent``）；仅当数据恰好契合下列形态时
    可用本工具省事。card_type 是布局名，不是业务关键词：
    - weather：多日/多指标面板
    - news：要点列表
    - metrics / ranking / summary / steps / comparison / board

    payload 为 JSON 字符串。示例（weather）::
        {"metrics":[{"label":"今日","value":"33°","sub":"雷阵雨"}],
         "days":[{"day":"周二","icon":"雷","weather":"雷阵雨","high":"33","low":"26"}]}
    示例（news）::
        {"items": [{"icon": "国", "title": "标题", "desc": "摘要", "tag": "要点"}]}

    Args:
        ctx: 工具执行上下文
        card_type: 布局类型（按数据形态选）
        title: 主标题
        payload: JSON 对象字符串
        meta: 副标题/数据时点

    Returns:
        文本确认，或 bot 不可用时的图片 bytes
    """
    if not title or not title.strip():
        return "渲染失败：title 不能为空"
    if card_type not in _CARD_TYPES:
        return f"渲染失败：未知 card_type={card_type}，可选: {', '.join(sorted(_CARD_TYPES))}"
    try:
        data = _parse_payload(payload)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return f"渲染失败：payload JSON 无效（{e}）"

    try:
        from gsuid_core.ai_core.mcp.pii import mask_pii, mask_pii_in_html

        is_group = _is_group_render_ctx(ctx)
        safe_title = mask_pii(title.strip()) if is_group else title.strip()
        safe_meta = mask_pii(meta.strip()) if is_group else meta.strip()
        html = _build_card_html(card_type, safe_title, safe_meta, data)
        if is_group:
            html = mask_pii_in_html(html)
        image_bytes = await render_html_to_bytes(
            html,
            max_width=720.0 * 2.0,
            dpi=192.0,
            image_format="png",
            default_font_size=16.0,
            root_max_width=720.0,
        )
        logger.info(t("log.ai.buildintools_html_rendering_succeeded_ok", p0=len(image_bytes)))
        return await _finish_image(ctx, image_bytes)
    except Exception as e:
        logger.exception(t("log.ai.buildintools_html_rendering", e=e))
        return f"渲染失败：{str(e)}。请调整内容后重试渲染工具；仍失败则只对用户说一两句角色短结论，禁止长列表当台词。"


def _sniff_image_mime(data: bytes) -> str:
    from gsuid_core.utils.image.image_io import sniff_image_mime

    return sniff_image_mime(data, allow_svg=True) or "image/png"


def _bytes_to_data_uri(data: bytes, mime: Optional[str] = None) -> str:
    m = mime or _sniff_image_mime(data)
    return f"data:{m};base64,{base64.b64encode(data).decode('ascii')}"


def has_transparency(im: "PILImage") -> bool:
    if im.mode in ("RGBA", "LA"):
        lo = im.getchannel("A").getextrema()[0]
        return isinstance(lo, (int, float)) and lo < 255
    return im.mode == "P" and "transparency" in im.info


def _shrink_raster_if_needed(data: bytes, *, max_side: int, max_bytes: int) -> bytes:
    """过大位图缩边 + 再编码（不透明图出 JPEG）；SVG 原样返回。解码失败返回原 bytes。"""
    if _sniff_image_mime(data) == "image/svg+xml":
        return data
    from io import BytesIO

    from PIL import Image, ImageOps, UnidentifiedImageError

    # 只兜外部图片解码失败（损坏字节 / 超大图）
    try:
        im = Image.open(BytesIO(data))
        im.load()
    except (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as e:
        logger.debug(t("log.ai.embed_image_for_html_shrink_skip", e=e))
        return data
    side_ok = max_side <= 0 or max(im.size) <= max_side
    if side_ok and len(data) <= max_bytes:
        return data
    im = ImageOps.exif_transpose(im)
    if not side_ok:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = BytesIO()
    if has_transparency(im):
        im.save(buf, format="PNG", optimize=True)
    else:
        im.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
    out = buf.getvalue()
    if len(out) > max_bytes and max_side > 128:
        return _shrink_raster_if_needed(out, max_side=max(128, max_side // 2), max_bytes=max_bytes)
    return out


async def _download_url_bytes(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> Tuple[Optional[bytes], Optional[str]]:
    """GET 图片 URL（带退避重试）。成功 (bytes, None)；失败 (None, 中文错误)。"""
    resp: httpx.Response | None = None
    last_err = ""
    for attempt in range(3):
        try:
            if client is not None and attempt == 0:
                resp = await client.get(url)
            else:
                async with httpx.AsyncClient(
                    timeout=_EMBED_HTTP_TIMEOUT,
                    follow_redirects=True,
                    headers={"User-Agent": _EMBED_USER_AGENT},
                ) as tmp_client:
                    resp = await tmp_client.get(url)
            break
        except httpx.HTTPError as e:
            last_err = f"下载失败: {type(e).__name__}: {e}"
            if attempt < 2:
                await asyncio.sleep(0.35 * (attempt + 1))
    if resp is None:
        return None, last_err or "下载失败: 网络不可达"
    if resp.status_code >= 400:
        return None, f"下载失败: HTTP {resp.status_code}"
    data = resp.content
    if not data:
        return None, "下载失败: 空响应"
    if len(data) > 5_000_000:
        return None, "下载失败: 文件超过 5MB"
    raw_ct = resp.headers["content-type"] if "content-type" in resp.headers else ""
    ctype = raw_ct.split(";")[0].strip().lower()
    if ctype and not (
        ctype.startswith("image/") or ctype in ("application/octet-stream", "binary/octet-stream") or "svg" in ctype
    ):
        if _sniff_image_mime(data) == "image/png" and not data.startswith(b"\x89PNG"):
            if data.lstrip()[:1] in (b"<", b"{") and b"<svg" not in data[:800].lower():
                return None, f"下载失败: Content-Type 非图片 ({ctype or 'unknown'})"
    return data, None


def _iconify_url(prefix: str, name: str, color: Optional[str], *, host: str = "https://api.iconify.design") -> str:
    """``<img>`` 里的 SVG 不吃 CSS color，着色只能让 Iconify 出图时填。"""
    url = f"{host}/{prefix}/{name}.svg"
    if color:
        url += f"?color=%23{color.lower()}"
    return url


def _normalize_icon_name(prefix: str, name: str) -> str:
    if prefix == "mdi" and name in _ICON_NAME_FALLBACKS:
        return _ICON_NAME_FALLBACKS[name]
    return name


def _store_iconify_raw(key: str, entry: tuple[int, int, int, int, str]) -> None:
    if len(_ICONIFY_RAW_CACHE) >= _ICONIFY_CACHE_MAX:
        _ICONIFY_RAW_CACHE.pop(next(iter(_ICONIFY_RAW_CACHE)))
    _ICONIFY_RAW_CACHE[key] = entry


def _render_iconify_svg(entry: tuple[int, int, int, int, str], color: str | None) -> bytes:
    left, top, w, h, body = entry
    hex_c = f"#{color.lower()}" if color else "currentColor"
    colored = body.replace("currentColor", hex_c) if color else body
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="{left} {top} {w} {h}">{colored}</svg>'
    )
    return svg.encode("utf-8")


def _fallback_icon_svg(color: str | None) -> bytes:
    """图标名不存在或网络彻底离线时合成同色系几何矢量徽章，杜绝空圆圈与破图块。"""
    hex_c = f"#{color.lower()}" if color else "#64748b"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none">'
        f'<circle cx="12" cy="12" r="8.2" stroke="{hex_c}" stroke-width="2" stroke-opacity="0.88"/>'
        f'<circle cx="12" cy="12" r="3.1" fill="{hex_c}"/></svg>'
    )
    return svg.encode("utf-8")


async def _prefetch_iconify_batch(sources: list[str], client: httpx.AsyncClient) -> None:
    """按 prefix 合并拉取整页 Iconify JSON，把 30+ 次单图标请求压缩为 1 次并支持多源容灾。"""
    by_prefix: dict[str, list[str]] = {}
    for src in sources:
        m = _ICON_SOURCE_RE.match(src.strip())
        if m is None:
            continue
        prefix = m.group("prefix").lower()
        name = _normalize_icon_name(prefix, m.group("name").lower())
        key = f"{prefix}/{name}"
        if key in _ICONIFY_RAW_CACHE:
            continue
        if prefix not in by_prefix:
            by_prefix[prefix] = []
        if name not in by_prefix[prefix]:
            by_prefix[prefix].append(name)

    for prefix, names in by_prefix.items():
        for start in range(0, len(names), 48):
            chunk = names[start : start + 48]
            query = ",".join(chunk)
            payload: dict[str, object] | None = None
            for host in _ICONIFY_API_HOSTS:
                url = f"{host}/{prefix}.json?icons={query}"
                try:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        parsed: object = resp.json()
                        if isinstance(parsed, dict):
                            payload = parsed
                            break
                except (httpx.HTTPError, ValueError):
                    continue
            if payload is None:
                continue
            def_w = payload["width"] if "width" in payload and isinstance(payload["width"], int) else 24
            def_h = payload["height"] if "height" in payload and isinstance(payload["height"], int) else 24
            icons_map = payload["icons"] if "icons" in payload and isinstance(payload["icons"], dict) else {}
            aliases_map = payload["aliases"] if "aliases" in payload and isinstance(payload["aliases"], dict) else {}
            for req_name in chunk:
                target = req_name
                if target not in icons_map and target in aliases_map:
                    al_obj = aliases_map[target]
                    if isinstance(al_obj, dict) and "parent" in al_obj and isinstance(al_obj["parent"], str):
                        target = al_obj["parent"]
                if target not in icons_map:
                    continue
                ic_obj = icons_map[target]
                if not isinstance(ic_obj, dict) or "body" not in ic_obj or not isinstance(ic_obj["body"], str):
                    continue
                left = ic_obj["left"] if "left" in ic_obj and isinstance(ic_obj["left"], int) else 0
                top = ic_obj["top"] if "top" in ic_obj and isinstance(ic_obj["top"], int) else 0
                iw = ic_obj["width"] if "width" in ic_obj and isinstance(ic_obj["width"], int) else def_w
                ih = ic_obj["height"] if "height" in ic_obj and isinstance(ic_obj["height"], int) else def_h
                _store_iconify_raw(f"{prefix}/{req_name}", (left, top, iw, ih, ic_obj["body"]))


async def _resolve_embed_source_bytes(
    source: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """解析嵌图来源 → (bytes, mime_hint, error)。"""
    from gsuid_core.utils.image.image_io import unwrap_image_url, decode_data_or_base64_uri

    raw = unwrap_image_url(source or "")
    if not raw:
        return None, None, "image_source 不能为空"

    # data URI / base64://
    if raw.startswith(("data:image/", "base64://")):
        decoded, dec_err = decode_data_or_base64_uri(raw)
        if dec_err is not None or decoded is None:
            return None, None, dec_err or "data URI 解析失败"
        return decoded, _sniff_image_mime(decoded), None

    # Iconify 矢量图标：优先命中批量预热缓存，未命中则轮询多镜像，缺名降级同色几何图标
    m = _ICON_SOURCE_RE.match(raw)
    if m is not None:
        prefix = m.group("prefix").lower()
        name = _normalize_icon_name(prefix, m.group("name").lower())
        color = m.group("color")
        raw_key = f"{prefix}/{name}"
        if raw_key in _ICONIFY_RAW_CACHE:
            return _render_iconify_svg(_ICONIFY_RAW_CACHE[raw_key], color), "image/svg+xml", None
        cache_url = _iconify_url(prefix, name, color)
        if cache_url in _ICONIFY_SVG_CACHE:
            return _ICONIFY_SVG_CACHE[cache_url], "image/svg+xml", None
        for host in _ICONIFY_API_HOSTS:
            data, err = await _download_url_bytes(_iconify_url(prefix, name, color, host=host), client=client)
            if err is None and data is not None and b"<svg" in data[:800].lower():
                if len(_ICONIFY_SVG_CACHE) >= _ICONIFY_CACHE_MAX:
                    _ICONIFY_SVG_CACHE.pop(next(iter(_ICONIFY_SVG_CACHE)))
                _ICONIFY_SVG_CACHE[cache_url] = data
                return data, "image/svg+xml", None
        return _fallback_icon_svg(color), "image/svg+xml", None

    # http(s)
    if raw.startswith("http://") or raw.startswith("https://"):
        data, err = await _download_url_bytes(raw, client=client)
        if err is not None:
            return None, None, err
        assert data is not None
        return data, _sniff_image_mime(data), None

    # Kanban artifact
    if raw.startswith("res_"):
        from gsuid_core.ai_core.buildin_tools.message_sender import _resolve_kanban_artifact

        payload = await _resolve_kanban_artifact(raw)
        if isinstance(payload, bytes):
            return payload, _sniff_image_mime(payload), None
        if isinstance(payload, str):
            return None, None, f"{raw} 是文本 artifact，不能嵌图；请用图片 URL 或 img_/图片 res_"
        # fall through to RM

    # RM img_ / 其它
    try:
        data = await RM.get(raw)
    except ValueError as e:
        if "找不到资源" in str(e):
            return None, None, f"找不到资源: {raw}"
        return None, None, f"读取资源失败: {e}"
    return data, _sniff_image_mime(data), None


def _needs_auto_embed(src: str) -> bool:
    """是否需要在渲染前解析为 data URI（通用前缀，无业务域特判）。"""
    s = (src or "").strip()
    if not s or s.startswith("data:"):
        return False
    if s.startswith(("http://", "https://", "icon:", "img_", "res_")):
        return True
    return False


async def _source_to_data_uri(
    source: str,
    *,
    max_side: int = _AUTO_EMBED_MAX_SIDE,
    client: httpx.AsyncClient | None = None,
) -> Tuple[Optional[str], Optional[str]]:
    """解析来源 → (data_uri, error)。"""
    data, mime, err = await _resolve_embed_source_bytes(source, client=client)
    if err is not None or data is None:
        return None, err or "未知错误"
    data = _shrink_raster_if_needed(data, max_side=max_side, max_bytes=_EMBED_MAX_BYTES)
    if len(data) > _EMBED_MAX_BYTES and _sniff_image_mime(data) != "image/svg+xml":
        return None, f"压缩后仍超过 {_EMBED_MAX_BYTES} bytes"
    mime_final = mime or _sniff_image_mime(data)
    if mime_final != "image/svg+xml":
        mime_final = _sniff_image_mime(data)
    return _bytes_to_data_uri(data, mime_final), None


def _collect_auto_embed_sources(html: str) -> list[str]:
    """收集 HTML 内需自动嵌图的唯一 src（位图与 icon: 矢量图标分桶限流）。"""
    seen: set[str] = set()
    images: list[str] = []
    icons: list[str] = []
    for regex in (_IMG_SRC_ATTR_RE, _CSS_URL_RE):
        for m in regex.finditer(html):
            src = m.group("src").strip()
            if not _needs_auto_embed(src) or src in seen:
                continue
            seen.add(src)
            if src.lower().startswith("icon:"):
                if len(icons) < _AUTO_EMBED_MAX_ICONS:
                    icons.append(src)
            elif len(images) < _AUTO_EMBED_MAX_IMAGES:
                images.append(src)
    return images + icons


async def _auto_embed_html_images(html: str) -> str:
    """渲染前：把 ``<img src>`` / ``url(...)`` 中的外链、icon:、img_/res_ 自动换成 data URI。

    **设计意图**：不依赖 agent 再调嵌图工具——写正常 HTML 即可一次出图。
    解析失败时用 1×1 透明像素占位，不阻断整张渲染。
    """
    sources = _collect_auto_embed_sources(html)
    if not sources:
        return html

    sem = asyncio.Semaphore(_AUTO_EMBED_CONCURRENCY)
    resolved: dict[str, str] = {}

    async with httpx.AsyncClient(
        timeout=_EMBED_HTTP_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _EMBED_USER_AGENT},
    ) as shared_client:
        await _prefetch_iconify_batch(sources, shared_client)

        async def _one(src: str) -> None:
            async with sem:
                uri, err = await _source_to_data_uri(src, client=shared_client)
                if uri is not None:
                    resolved[src] = uri
                    logger.info(t("log.ai.auto_embed_ok", src=repr(src[:96]), uri_len=len(uri)))
                else:
                    resolved[src] = _TRANSPARENT_1PX_PNG
                    logger.warning(t("log.ai.auto_embed_fail", src=repr(src[:96]), err=err))

        await asyncio.gather(*(_one(s) for s in sources))

    def _repl_img(m: re.Match[str]) -> str:
        src = m.group("src").strip()
        if src in resolved:
            return f"{m.group('pre')}{m.group('q')}{resolved[src]}{m.group('q')}"
        return m.group(0)

    def _repl_css(m: re.Match[str]) -> str:
        src = m.group("src").strip()
        if src in resolved:
            q = m.group("q") or '"'
            return f"url({q}{resolved[src]}{q})"
        return m.group(0)

    out = _IMG_SRC_ATTR_RE.sub(_repl_img, html)
    out = _CSS_URL_RE.sub(_repl_css, out)
    return out


_PHOTO_HANDLE_RE = re.compile(r"""src\s*=\s*["']img_""", re.IGNORECASE)


def embed_photo_block_reason(html: str, *, embed_on: bool, searched: bool, found: bool) -> str | None:
    """嵌入配图开启且显式选用图文画册版式（feature / gallery / magazine）时，强制先搜并嵌入照片。

    自由定制 HTML（未声明 ``layout-*``）或数据/对比/架构版式不强制照片，允许纯图表与图标排版。
    """
    if not embed_on or _PHOTO_HANDLE_RE.search(html):
        return None
    layout = page_layout(html)
    if layout is None or layout not in PHOTO_LAYOUTS:
        return None
    if not searched:
        return (
            f"layout-{layout} 需要实景/对象照片，还没有拉图。"
            "先调一次 find_images（主题图 + 每个主要对象一个词），把返回的 img_ 句柄写进 HTML 再渲染。"
            "task 里写图源缺失、官方图拉不到，仍然要搜。"
        )
    if found:
        return (
            "find_images 已经返回了 img_ 句柄，HTML 里一张都没用。"
            "把句柄写进照片槽位（gs-hero / gs-split-media / gs-cover / gs-thumb）后再渲染。"
        )
    return None


def _is_group_render_ctx(ctx: RunContext[ToolContext]) -> bool:
    """判断当前渲染调用是否处于群聊会话（含群聊派生的 Kanban 子任务）。"""
    deps = ctx.deps
    if isinstance(deps, ToolContext) and deps.ev is not None:
        return bool(deps.ev.group_id)
    from gsuid_core.ai_core.planning.runtime import get_plan_context

    plan = get_plan_context()
    if plan is not None and plan.scope_key:
        return plan.scope_key.startswith("group:")
    return False


# 与 card/md 同属 media：主人格不保底；由 render_agent 白名单持有
@ai_tools(category="media", capability_domain="资料出图", code_callable=False)
async def render_html_to_image(
    ctx: RunContext[ToolContext],
    html_content: str,
    image_format: Literal["png", "jpeg"] = "png",
    max_width: int = 840,
) -> str | bytes:
    """将 HTML 渲染为高质感编辑级信息图长图并自动发送。**render_agent 出图主工具**。

    主人格应 ``create_subagent(agent_profile="render_agent", task=...)`` 委派，勿自行堆 HTML。
    整份事实包只渲**一张**：多区块写在同一 HTML 里，不要连调本工具。

    ## 视觉与版式自由度（两种路径均可，鼓励按主题创意定制）
    1. **自由定制编辑级信息图**：自写完整 ``<style>`` 定制配色、渐变、圆角卡片、巨型核心指标（68–88px）、
       对比高亮框（accent border + 柔和 box-shadow）与多栏网格；深色（#07090e 曜石黑 + 青/紫/金霓虹强调）
       与浅色（#f8fafc 杂志纸感白 + 深石板字 + 高饱和语义色）均可按内容气质自由选择。
    2. **gs- 组件加速混排**：外层可选 ``<div class="gs-page layout-xxx theme-xxx">``，用到 ``gs-`` 类时自动前置注入
       基础 CSS（paper / ocean / forest / sunset / sakura / violet / slate 浅色；ink / obsidian 暗色），
       自写 ``<style>`` 排在后面可任意覆盖扩展。

    ## 图标、照片与图表
    - **语义图标（强烈推荐）**：章节标题、指标卡、对比项、提示框积极使用彩色 Iconify 图标：
      ``<img src="icon:mdi/compass-outline?color=0284c7" width="22" height="22">``（颜色不带 #）。
    - **实景照片**：旅游/美食/人物/展品等具象题材且开启嵌入配图时，先调 ``find_images`` 拿 ``img_`` 句柄放入带
      ``border-radius`` + ``overflow:hidden`` 的容器；抽象概念/数据分析/技术原理解析无需强凑无关网图。
    - **数据图表**：≥3 个可比数值先调 ``render_chart_spec``（可选 ``benchmark`` 胶囊对标条、``levels`` 非零锚定
      关键位阶梯、``bar``/``hbar`` 正负比例柱、``line`` 趋势线、``donut`` 环形占比），直接把返回头注释里的
      ``{{chart_xxxxxxxx}}`` 句柄或完整 ``<svg>`` 放进卡片（严禁自造 ``__CHART_*__`` 占位符）。
    - **状态机 / 架构流程 / 时序图**：支持 ``<pre class="mermaid">stateDiagram-v2 ...</pre>``
      （服务端自动转高清矢量图），也可手写精美的内联 ``<svg>`` 或 HTML Flex 状态流卡片呈现机制细节。

    ## Takumi 引擎硬红线（违者排版崩坏）
    - 逻辑宽默认 840（上限 1000），整图高宽比 ≤ 4.0，整页必须有不透明底色，深浅字色高对比自洽。
    - **严禁**使用 ``:first-child`` / ``:last-child``（Takumi 不支持，必失效）；间距用 ``gap`` 或相邻兄弟 ``+``。
    - **防两字竖排挤压**：左右横排（flex row）中，左侧标签/键名/图标容器**必须**写 ``flex:none; white-space:nowrap;``！
    - **防贴边零留白**：带边框或底色的卡片内部必须有 ``padding: 20px 22px``，
      纵向堆叠容器必须设 ``display:flex; flex-direction:column; gap:16px``。
    - 字重 MiSans VF 轴 150–700（正文 400、小标题 600、主标题/大数字 700；800/900 无效）。无 JS / float / 伪元素排版。

    Args:
        ctx: 工具执行上下文
        html_content: 完整 HTML 文档或带 ``<style>`` 的片段
        image_format: 默认 png
        max_width: 逻辑宽，默认 840，硬上限 1000

    Returns:
        文本确认，或 bot 不可用时的图片 bytes
    """
    if not html_content or not html_content.strip():
        return "渲染失败：HTML内容不能为空"

    html_content = resolve_chart_placeholders(html_content)
    probe_body = re.sub(r"<(pre|code)\b[^>]*>.*?</\1>", "", html_content, flags=re.I | re.S)
    unresolved_m = _UNRESOLVED_CHART_PLACEHOLDER_RE.search(probe_body)
    if unresolved_m is not None:
        tok = unresolved_m.group(0)[:48]
        return (
            f"⚠️ 渲染中止（未消耗出图额度）：HTML 含未展开的图表占位符 `{tok}`。"
            "请改用 render_chart_spec 返回头注释中的 `{{chart_xxxxxxxx}}` 句柄或内联真实 `<svg>…</svg>` 后，"
            "重新调用一次 render_html_to_image。"
        )

    from gsuid_core.ai_core.configs.ai_config import render_embed_images_enabled

    searched = False
    found = False
    deps = ctx.deps
    if isinstance(deps, ToolContext):
        if "find_images_ran" in deps.extra:
            searched = deps.extra["find_images_ran"] is True
        if "find_images_found" in deps.extra:
            found = deps.extra["find_images_found"] is True
    blocked = embed_photo_block_reason(
        html_content,
        embed_on=render_embed_images_enabled(),
        searched=searched,
        found=found,
    )
    if blocked:
        return blocked

    try:
        from gsuid_core.ai_core.mcp.pii import mask_pii_in_html

        if _is_group_render_ctx(ctx):
            html_content = mask_pii_in_html(html_content)
        # 先自动嵌图（外链/icon/资源），再 table 改写与引擎卫生 CSS
        html = await _auto_embed_html_images(html_content)
        html = _prepare_free_html(html)
        logical_w = _clamp_logical_width(max_width)
        _dpr = 2.0
        image_bytes = await render_html_to_bytes(
            html,
            max_width=float(logical_w) * _dpr,
            dpi=96.0 * _dpr,
            image_format=image_format,
            default_font_size=16.0,
            root_max_width=float(logical_w),
        )
        logger.info(t("log.ai.buildintools_html_rendering_succeeded_ok", p0=len(image_bytes)))
        return await _finish_image(ctx, image_bytes)
    except Exception as e:
        logger.exception(t("log.ai.buildintools_html_rendering", e=e))
        # 明确失败协议：禁止模型把「将就文字版长列表」当成功交付
        return (
            f"渲染失败：{str(e)}。"
            "请精简 HTML 后重试 render_html_to_image，或改用更短的 render_card；"
            "仍失败则只对用户说一两句角色短结论，禁止输出长列表/多条数据当台词。"
        )


@ai_tools(category="media", capability_domain="资料出图", code_callable=False)
async def render_markdown_to_image(
    ctx: RunContext[ToolContext],
    title: str,
    markdown_content: str,
    image_format: Literal["png", "jpeg"] = "png",
    max_width: int = 900,
) -> str | bytes:
    """将 Markdown 渲染为暗色卡片图。纯文字列表/步骤的次选。

    需要自定义布局、指标卡、时间线时优先 ``render_html_to_image``（``render_agent``）。
    ≥3 条数据点时必须出图；角色台词只留一两句引导。
    mermaid 代码块会向 mermaid.ink 取 SVG 再出图。服务不可用时该块变成黑底红字，其余照常出图。

    Args:
        ctx: 工具执行上下文
        title: 图片标题
        markdown_content: Markdown 正文（不含一级标题）
        image_format: 默认 png
        max_width: 默认 900，硬上限 1000

    Returns:
        文本确认，或 bot 不可用时的图片 bytes
    """
    if not markdown_content or not markdown_content.strip():
        return "渲染失败：内容不能为空"

    try:
        from gsuid_core.ai_core.mcp.pii import mask_pii

        md = f"# {title}\n\n{markdown_content.strip()}{_footer()}"
        if _is_group_render_ctx(ctx):
            md = mask_pii(md)
        logical_w = _clamp_logical_width(max_width)
        _dpr = 2.0
        image_bytes = await render_md_to_bytes(
            md=md,
            css_path=_MD_CSS_PATH,
            max_width=int(logical_w * _dpr),
            dpi=96.0 * _dpr,
            image_format=image_format,
            dark=False,
            root_max_width=float(logical_w),
        )
        logger.info(t("log.ai.buildintools_markdown_rendering_succeeded_ok", p0=len(image_bytes)))
        return await _finish_image(ctx, image_bytes)
    except Exception as e:
        logger.exception(t("log.ai.buildintools_markdown_rendering", e=e))
        return f"渲染失败：{str(e)}"
