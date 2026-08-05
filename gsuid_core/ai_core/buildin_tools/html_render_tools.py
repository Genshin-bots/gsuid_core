"""HTML渲染工具模块

提供将 HTML / Markdown / 结构化卡片渲染为图片的能力，供 AI 调用。
渲染成功后自动通过 bot 发送图片；bot 不可用时回传 bytes 供 agent loop 注册资源。
"""

import re
import json
import base64
import asyncio
import datetime
from typing import Any, Tuple, Literal, Optional
from pathlib import Path

import httpx
from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.html_render import im_templates, render_md_to_bytes, render_html_to_bytes
from gsuid_core.utils.resource_manager import RM

# 嵌图上限：防 agent 把数 MB 原图塞进 HTML 撑爆上下文
_EMBED_MAX_BYTES = 450_000
_EMBED_HTTP_TIMEOUT = 12.0
_EMBED_USER_AGENT = "GsCore-HTMLEmbed/1.0 (+local-render)"
# 单次 HTML 自动嵌图：并发与数量上限（通用，不按业务域特判）
_AUTO_EMBED_MAX_IMAGES = 32
_AUTO_EMBED_MAX_SIDE = 512
_AUTO_EMBED_CONCURRENCY = 6
# Iconify 按需单图标（约 1KB），替代本地 100MB 图标包
_ICONIFY_SVG_URL = "https://api.iconify.design/{prefix}/{name}.svg"
_ICON_SOURCE_RE = re.compile(
    r"^icon:(?P<prefix>[a-z0-9-]+)/(?P<name>[a-z0-9-]+)$",
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
# 解析失败时的 1×1 透明 PNG，避免布局塌成破图占位文字
_TRANSPARENT_1PX_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
# Takumi 引擎卫生 CSS：无主题、无业务域；修常见引擎坑
# 注意：不在此强制 body 背景色（避免盖掉 agent 浅色主题）；透明 PNG 由
# ``_ensure_opaque_image_bytes`` 在出图后合成实色底。
_TAKUMI_ENGINE_HYGIENE_CSS = """
/* engine hygiene — not a visual theme */
img{max-width:100%;height:auto;}
/* chips: box grows with text; content centered in bg; horizontal CJK only */
.tag,.badge,.chip,.pill,.label{
  display:inline-block;box-sizing:border-box;
  padding:2px 8px;line-height:1.35;width:auto;max-width:100%;
  text-align:center;vertical-align:middle;
  white-space:nowrap;word-break:keep-all;overflow:visible;
  writing-mode:horizontal-tb;text-orientation:mixed;
}
/* brand/logo: single horizontal run, not stacked glyphs in a square */
.logo,.brand,.brand-mark,.icon-label{
  writing-mode:horizontal-tb;white-space:nowrap;word-break:keep-all;
  text-align:center;
}
"""

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
h1{{font-size:26px;font-weight:900;color:#fff;margin:0 0 6px;line-height:1.25;}}
h2{{
  font-size:15px;font-weight:800;color:#e9eef7;margin:22px 0 12px;
  border-left:4px solid #5b9dd9;padding-left:10px;
}}
.meta{{font-size:12px;color:#6b7890;margin-bottom:16px;letter-spacing:0.02em;}}
.grid,.mgrid{{display:flex;gap:12px;margin:8px 0 4px;}}
.metric{{
  width:220px;background:linear-gradient(160deg,#1a2740,#131d30);
  border:1px solid rgba(91,157,217,0.14);border-radius:16px;
  padding:16px 14px;border-top:3px solid #5b9dd9;
  box-shadow:0 4px 18px rgba(0,0,0,0.32);
}}
.metric .lab,.lab{{font-size:11px;color:#6b7890;font-weight:700;letter-spacing:0.08em;}}
.metric .val,.val{{font-size:30px;font-weight:900;color:#fff;margin:6px 0 4px;line-height:1.05;}}
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
  font-size:14px;font-weight:900;color:#0a1220;
  background:linear-gradient(145deg,#5b9dd9,#2a5f9a);
  box-shadow:0 3px 10px rgba(0,0,0,0.35);
}}
.ico.storm{{background:linear-gradient(145deg,#9d8cff,#5b4bb5);}}
.ico.rain{{background:linear-gradient(145deg,#5b9dd9,#2a5f9a);}}
.ico.cloud{{background:linear-gradient(145deg,#8fa3bf,#4a5d75);}}
.ico.sun{{background:linear-gradient(145deg,#e8b45a,#b07a28);}}
.ico.hot{{background:linear-gradient(145deg,#ef7d6c,#b04538);}}
.day .w,.w{{font-size:11px;color:#9aa7bd;margin-top:8px;text-align:center;}}
.day .t .hi,.hi{{color:#fff;font-weight:800;}}
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
  font-size:14px;font-weight:900;color:#0a1220;
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
  font-size:11px;font-weight:800;background:#5b9dd9;color:#0a1220;
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
  font-size:11px;font-weight:800;background:#3ec9a7;color:#0a1220;
}}
.muted{{color:#8a9bb5;font-size:12px;}}
.big{{font-size:34px;font-weight:900;color:#fff;line-height:1.05;}}
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
.cval{{width:58px;flex:none;font-size:12px;font-weight:800;color:#fca5a5;}}
.cval.up{{color:#3ec9a7;}}
</style></head>
<body>
{body}
<div style="margin:20px 4px 16px;padding-top:12px;border-top:1px solid rgba(91,157,217,0.08);
  font-size:10px;color:#3d5068;letter-spacing:0.03em;">
  AI 生成资料 · 数据可能滞后 · 仅供参考 · {ts}</div>
</body></html>"""

_RENDER_OK = "图片已发送（{kb}KB）。台词只留一两句引导，禁止复述数据。"

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


def _prepare_free_html(html_content: str) -> str:
    """自由 HTML 预处理：不套设计壳；table 改写；注入引擎卫生 CSS（无主题）。"""
    html = html_content.strip()
    if not html:
        return html
    html = _rewrite_html_tables(html)
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
    """尝试通过 bot 直接发送图片。成功返回 True。"""
    bot = ctx.deps.bot
    if bot is None:
        return False
    try:
        await bot.send(MessageSegment.image(image_bytes))
        return True
    except Exception as e:
        logger.debug(t("log.htmlrender.auto_send_fallback", e=e))
        return False


async def _finish_image(ctx: RunContext[ToolContext], image_bytes: bytes) -> str | bytes:
    """不透明化 → 发送；同一 Kanban 任务默认只成功推送一张（防多段刷屏）。

    bot 不可用时回传 bytes，由 agent loop 注册资源 ID。
    """
    from gsuid_core.ai_core.planning.runtime import get_plan_context

    image_bytes = _ensure_opaque_image_bytes(image_bytes)

    plan = get_plan_context()
    task_id = plan.task_id if plan is not None else ""

    if task_id and task_id in _RENDER_EMITTED_TASKS:
        return (
            "⚠️ 本任务已成功出过图，本次未再推送。"
            "请把全部区块合并进**一张**完整 HTML，只调用一次 render_html_to_image；"
            "不要按章节拆成多次渲染。用户未明确要求多页时禁止连渲多张。"
        )

    if await _try_send_image(ctx, image_bytes):
        _mark_render_emitted(task_id)
        return _RENDER_OK.format(kb=len(image_bytes) // 1024)
    # bot 不可用：仍标记，避免同一任务连续返回多份 bytes 被下游连发
    _mark_render_emitted(task_id)
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


@ai_tools(category="media", capability_domain="资料出图")
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
        html = _build_card_html(card_type, title.strip(), meta.strip(), data)
        image_bytes = await render_html_to_bytes(
            html,
            max_width=720.0 * 2.0,
            dpi=192.0,
            image_format="png",
            default_font_size=15.0,
            root_max_width=720.0,
        )
        logger.info(t("log.ai.buildintools_html_rendering_succeeded_ok", p0=len(image_bytes)))
        return await _finish_image(ctx, image_bytes)
    except Exception as e:
        logger.exception(t("log.ai.buildintools_html_rendering", e=e))
        return f"渲染失败：{str(e)}。请调整内容后重试渲染工具；仍失败则只对用户说一两句角色短结论，禁止长列表当台词。"


def _sniff_image_mime(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    head = data[:200].lstrip().lower()
    if head.startswith(b"<svg") or b"<svg" in data[:500].lower():
        return "image/svg+xml"
    return "image/png"


def _bytes_to_data_uri(data: bytes, mime: Optional[str] = None) -> str:
    m = mime or _sniff_image_mime(data)
    return f"data:{m};base64,{base64.b64encode(data).decode('ascii')}"


def _shrink_raster_if_needed(data: bytes, *, max_side: int, max_bytes: int) -> bytes:
    """过大位图缩边 + 再编码；SVG 原样返回。失败则返回原 bytes。"""
    mime = _sniff_image_mime(data)
    if mime == "image/svg+xml":
        return data
    if len(data) <= max_bytes and max_side <= 0:
        return data
    try:
        from io import BytesIO

        from PIL import Image

        im = Image.open(BytesIO(data))
        im.load()
        if max_side > 0 and max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        buf = BytesIO()
        out_fmt = "PNG" if im.mode in ("RGBA", "P") or mime == "image/png" else "JPEG"
        if out_fmt == "JPEG" and im.mode in ("RGBA", "P"):
            im = im.convert("RGB")
        save_kw: dict[str, Any] = {"optimize": True}
        if out_fmt == "JPEG":
            save_kw["quality"] = 85
        im.save(buf, format=out_fmt, **save_kw)
        out = buf.getvalue()
        # 仍超限则更激进缩边
        if len(out) > max_bytes and max_side > 128:
            return _shrink_raster_if_needed(out, max_side=max(128, max_side // 2), max_bytes=max_bytes)
        return out
    except Exception as e:
        logger.debug(t("log.ai.embed_image_for_html_shrink_skip", e=e))
        return data


async def _download_url_bytes(url: str) -> Tuple[Optional[bytes], Optional[str]]:
    """GET 图片 URL。成功 (bytes, None)；失败 (None, 中文错误)。"""
    try:
        async with httpx.AsyncClient(
            timeout=_EMBED_HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _EMBED_USER_AGENT},
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        return None, f"下载失败: {type(e).__name__}: {e}"
    if resp.status_code >= 400:
        return None, f"下载失败: HTTP {resp.status_code}"
    data = resp.content
    if not data:
        return None, "下载失败: 空响应"
    if len(data) > 5_000_000:
        return None, "下载失败: 文件超过 5MB"
    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype and not (
        ctype.startswith("image/") or ctype in ("application/octet-stream", "binary/octet-stream") or "svg" in ctype
    ):
        # 部分 CDN 不给 image/*；靠魔数兜底
        if _sniff_image_mime(data) == "image/png" and not data.startswith(b"\x89PNG"):
            # default sniff may false-positive; check for html error page
            if data.lstrip()[:1] in (b"<", b"{") and b"<svg" not in data[:800].lower():
                return None, f"下载失败: Content-Type 非图片 ({ctype or 'unknown'})"
    return data, None


async def _resolve_embed_source_bytes(
    source: str,
) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """解析嵌图来源 → (bytes, mime_hint, error)。"""
    raw = (source or "").strip()
    if not raw:
        return None, None, "image_source 不能为空"

    # data URI
    if raw.startswith("data:image/"):
        try:
            header, b64 = raw.split(",", 1)
            mime = header.split(";")[0].split(":", 1)[1]
            return base64.b64decode(b64), mime, None
        except Exception as e:
            return None, None, f"data URI 解析失败: {e}"

    # Iconify 按需单图标：icon:prefix/name
    m = _ICON_SOURCE_RE.match(raw)
    if m is not None:
        prefix = m.group("prefix").lower()
        name = m.group("name").lower()
        url = _ICONIFY_SVG_URL.format(prefix=prefix, name=name)
        data, err = await _download_url_bytes(url)
        if err is not None or data is None:
            return None, None, f"图标拉取失败 ({prefix}/{name}): {err or '空响应'}"
        if b"<svg" not in data[:800].lower():
            return None, None, f"图标拉取失败: 非 SVG 响应 ({prefix}/{name})"
        return data, "image/svg+xml", None

    # http(s)
    if raw.startswith("http://") or raw.startswith("https://"):
        data, err = await _download_url_bytes(raw)
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
) -> Tuple[Optional[str], Optional[str]]:
    """解析来源 → (data_uri, error)。"""
    data, mime, err = await _resolve_embed_source_bytes(source)
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
    """收集 HTML 内需自动嵌图的唯一 src（img + CSS url），保序去重。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for m in _IMG_SRC_ATTR_RE.finditer(html):
        src = m.group("src").strip()
        if _needs_auto_embed(src) and src not in seen:
            seen.add(src)
            ordered.append(src)
    for m in _CSS_URL_RE.finditer(html):
        src = m.group("src").strip()
        if _needs_auto_embed(src) and src not in seen:
            seen.add(src)
            ordered.append(src)
    return ordered[:_AUTO_EMBED_MAX_IMAGES]


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

    async def _one(src: str) -> None:
        async with sem:
            uri, err = await _source_to_data_uri(src)
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


# 与 card/md 同属 media：主人格不保底；由 render_agent 白名单持有
@ai_tools(category="media", capability_domain="资料出图")
async def render_html_to_image(
    ctx: RunContext[ToolContext],
    html_content: str,
    image_format: Literal["png", "jpeg"] = "png",
    max_width: int = 800,
) -> str | bytes:
    """将自定义 HTML 渲染为高清图片并自动发送。**render_agent 出图主工具**。

    主人格应 ``create_subagent(agent_profile="render_agent", task=...)`` 委派，勿自行堆 HTML。
    **自由创作，无固定业务模板。** 按内容自写 HTML + CSS，不要套固定「指标卡 + 横条」。
    系统**不会**再自动套暗色设计壳。

    ## 写法
    - 推荐完整文档：``<!DOCTYPE html><html><head><style>...</style></head><body>...</body></html>``
    - 也可只写片段 + 内联 ``<style>``；视口宽 = max_width，高度随内容自适应
    - **整页不透明背景**（必写）：先选**暗色或浅色**主题，再写 page/text 成套 color；
      禁止透明/缺省底（会出透明 PNG）。``#0f172a`` 只是暗色示例，不是唯一合法值。
      - 暗色例：``html,body{{background:#0f172a;color:#e2e8f0;}}`` 且 h1/标题显式浅色
      - 浅色例：``html,body{{background:#f4f6fa;color:#1a2332;}}`` 且标题深色
    - **默认整份事实包只渲一张**：多区块写在同一 HTML 里，不要连调本工具多次
    - 中文字体用 ``"MiSans","PingFang SC","Microsoft YaHei",sans-serif``
    - 布局优先 flex / 基础 grid；可用原生 ``<table>``（引擎侧自动改写为 flex 网格）
    - 多项对比/列表/面板：用表格或自写卡片均可，禁止把长数据念成台词
    - **对比度（暗色必看）**：深色页底时，h1/h2/.title/.headline/**正文** **必须显式**浅色
      （如 ``#edf4ff`` / ``#e2e8f0``）；禁止只写字号不写颜色、禁止暗底深字/默认黑；
      卡片用略亮的不透明 surface，勿靠全透明层。浅色主题则主文字须足够深。
      font-weight 用 400~900
    - **颜色语义**：同一页一套语义色（如 ↑/↓ 各一色）。暗底上强调色用偏亮的绿/红/金；
      浅底上用略深的语义色。**class 优先级**：``.item .value.up`` 须能盖过 ``.item .value``，
      禁止基类 ``color`` 盖掉修饰 class；状态色优先单 class
    - **短标签**（chip/badge）：``display:inline-block;text-align:center;white-space:nowrap``；
      父行居中用 ``.row{text-align:center}`` 或 ``display:flex;justify-content:center``
    - **插图 / 图标（一次写完即可）**：直接在 HTML 里写，**系统渲染前自动嵌成 data URI**，
      无需另调工具：
      - ``<img src="https://...">`` 外链图
      - ``<img src="icon:mdi/chart-line">`` 按需矢量图标（~1KB，非本地图标包）
      - ``<img src="img_xxx">`` / ``res_xxx`` 资源池图
      - CSS ``background-image:url(https://...|icon:...|img_...)`` 同样自动嵌
      - 已是 ``data:image/...`` 的原样保留
      禁止用色块汉字冒充 logo；少用 emoji 当图标，需要图就写 ``<img>``。

    ## 表格硬约束（引擎无 CSS table 模型，改写后是 div）
    - **禁原生 rowspan/colspan**：请展平为单层表（重复行）或用
      ``im_templates.comparison_card`` / ``board_card``；改写器仅近似占位。
    - **禁 ``td.xxx`` / ``th.xxx`` 选择器**：改写后节点是 ``div.md-table-cell``，
      请写 ``.xxx`` 或内联 ``style="text-align:right"``。
    - **数值列**：``4.33x`` / ``~16.5x`` / ``±`` 等单位请用内联 ``style="text-align:right"``
      或 class ``right``（不要依赖仅匹配 ``td`` 的选择器）。
    - **窄列 CJK**：默认可逐字断行；短标签靠卫生 CSS；普通格请 ``word-break:keep-all``，
      禁止手写 ``农<br>业<br>种<br>植`` 式竖排。
    - **字号**：正文建议 ≥14px，辅助 ≥12px；勿用 10–11px 全文。

    ## 示例（暗色；浅色时把 page/text/title 换成浅色 token 即可，勿当唯一模板）::

        <!DOCTYPE html><html><head><meta charset="utf-8"><style>
        /* 暗色 token 示例——可改为浅色：page #f4f6fa / text #1a2332 / title #0b1220 */
        html,body{font-family:"MiSans","Microsoft YaHei",sans-serif;padding:24px;
          background:#0f172a;color:#e2e8f0;}
        h1{font-size:22px;margin:0 0 12px;color:#edf4ff;font-weight:800;}
        .card{background:#1a2438;border-radius:12px;padding:12px;}
        .row{display:flex;gap:12px;align-items:center;}
        .tag{display:inline-block;padding:2px 8px;border-radius:4px;background:#334155;
             white-space:nowrap;text-align:center;}
        img.icon{width:28px;height:28px;border-radius:6px;}
        table{width:100%;border-collapse:collapse;}
        th,td{padding:8px 10px;border-bottom:1px solid #334155;text-align:left;color:#e2e8f0;}
        th{color:#94a3b8;font-size:12px;} .up{color:#34d399;} .right{text-align:right;color:#fde68a;}
        </style></head><body>
        <div class="row">
          <img class="icon" src="icon:mdi/chart-line" alt="" />
          <h1>持仓明细</h1>
        </div>
        <table><tr><th>名称</th><th class="right">浮盈</th></tr>
        <tr><td>长江电力</td><td class="up right">+5.48%</td></tr></table>
        </body></html>

    Args:
        ctx: 工具执行上下文
        html_content: 完整 HTML 或带样式的片段（按内容自由设计）
        image_format: 默认 png
        max_width: 默认 800

    Returns:
        文本确认，或 bot 不可用时的图片 bytes
    """
    if not html_content or not html_content.strip():
        return "渲染失败：HTML内容不能为空"

    try:
        # 先自动嵌图（外链/icon/资源），再 table 改写与引擎卫生 CSS
        html = await _auto_embed_html_images(html_content)
        html = _prepare_free_html(html)
        _dpr = 2.0
        image_bytes = await render_html_to_bytes(
            html,
            max_width=float(max_width) * _dpr,
            dpi=96.0 * _dpr,
            image_format=image_format,
            default_font_size=15.0,
            root_max_width=float(max_width),
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


@ai_tools(category="media", capability_domain="资料出图")
async def render_markdown_to_image(
    ctx: RunContext[ToolContext],
    title: str,
    markdown_content: str,
    image_format: Literal["png", "jpeg"] = "png",
    max_width: int = 800,
) -> str | bytes:
    """将 Markdown 渲染为暗色卡片图。纯文字列表/步骤的次选。

    需要自定义布局、指标卡、时间线时优先 ``render_html_to_image``（``render_agent``）。
    ≥3 条数据点时必须出图；角色台词只留一两句引导。

    Args:
        ctx: 工具执行上下文
        title: 图片标题
        markdown_content: Markdown 正文（不含一级标题）
        image_format: 默认 png
        max_width: 默认 800

    Returns:
        文本确认，或 bot 不可用时的图片 bytes
    """
    if not markdown_content or not markdown_content.strip():
        return "渲染失败：内容不能为空"

    try:
        md = f"# {title}\n\n{markdown_content.strip()}{_footer()}"
        _dpr = 2.0
        image_bytes = await render_md_to_bytes(
            md=md,
            css_path=_MD_CSS_PATH,
            max_width=int(max_width * _dpr),
            dpi=96.0 * _dpr,
            image_format=image_format,
            dark=False,
            root_max_width=float(max_width),
        )
        logger.info(t("log.ai.buildintools_markdown_rendering_succeeded_ok", p0=len(image_bytes)))
        return await _finish_image(ctx, image_bytes)
    except Exception as e:
        logger.exception(t("log.ai.buildintools_markdown_rendering", e=e))
        return f"渲染失败：{str(e)}"
