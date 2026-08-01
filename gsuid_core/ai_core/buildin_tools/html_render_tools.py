"""HTML渲染工具模块

提供将 HTML / Markdown / 结构化卡片渲染为图片的能力，供 AI 调用。
渲染成功后自动通过 bot 发送图片；bot 不可用时回传 bytes 供 agent loop 注册资源。
"""

import json
import datetime
from typing import Any, Literal
from pathlib import Path

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.html_render import im_templates, render_md_to_bytes, render_html_to_bytes

_MD_CSS_PATH = str(Path(__file__).resolve().parent.parent.parent / "utils" / "html_render" / "markdown_dark.css")

_FOOTER_TEMPLATE = "\n\n---\n\n> AI 生成资料 · 数据可能滞后 · 仅供参考 · {ts}"

# 暗色可视化壳：自带设计系统 class，agent 写片段时不必手写完整 CSS
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


def _wrap_html_if_needed(html_content: str) -> str:
    """缺完整文档结构时套暗色设计系统壳。"""
    html = html_content.strip()
    lower = html[:200].lower()
    if lower.startswith("<!doctype") or lower.startswith("<html"):
        return html
    return _HTML_SHELL.format(
        body=html,
        ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


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
    """发送成功返回确认串；bot 不可用时回传 bytes，由 agent loop 注册资源 ID。"""
    if await _try_send_image(ctx, image_bytes):
        return _RENDER_OK.format(kb=len(image_bytes) // 1024)
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

    多数据点出图**首选** ``render_html_to_image`` 自写布局；仅当数据恰好契合下列形态时
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


# 仅 HTML 进保底：多数据点出图主路径；card/md 走 media 按需召回，避免每轮 3 个 schema
@ai_tools(category="buildin", capability_domain="资料出图")
async def render_html_to_image(
    ctx: RunContext[ToolContext],
    html_content: str,
    image_format: Literal["png", "jpeg"] = "png",
    max_width: int = 800,
) -> str | bytes:
    """将自定义 HTML 渲染为高清图片并自动发送。**多数据点出图首选**。

    按内容自由设计布局（面板/时间线/对比/看板/资讯流等），模板覆盖不了的场景也能表达。
    推荐只写 body 片段，系统会套暗色设计系统壳（视口宽 = max_width，高度自适应）。

    ## 壳内 class（直接拿来用，结构必须匹配；**禁止在自己的 <style> 里重定义壳类**，会撞坏布局）
    - .grid>.metric：指标卡组，metric 内只放 .lab（小标签）/.val（大数字）/.sub（补充说明）
    - .row：横条信息卡 = .ico.sm（圆形文字图标，1 个字）+ .main（内含 .title/.desc）
      + 可选 .tag（右侧彩色标签，变体 .green/.gold/.red）
    - .chart>.crow：条形图行 = .clab（标签）+ .track>.fill（内联 style="width:百分比%"）
      + .cval（数值）；上涨/正向用 .fill.up 与 .cval.up
    - .item：纯文字条目卡；.pill：胶囊标签；.bar：渐变分隔条；.ico：圆形文字图标（变体 .ok/.warn/.bad/.news/.cool）
    - h1/h2/.meta/.big/.muted：标题/元信息/大数字/弱文字；.day 仅供天气日期卡，勿当通用容器
    - 确需自定义样式时：只用**新**类名（建议 my- 前缀），绝不覆盖壳类

    ## 视觉要求（必须遵守）
    - ≥3 个可比较的数值（涨跌/占比/排名/进度）→ 必须画 .chart 条形图；绝对值指标用 .metric 卡；禁止纯文字列表凑数
    - 每条 .row 信息行必须带 .ico.sm 文字图标；涨跌类标签用 .tag.green/.tag.red
    - 关键数字大且重（.val/.big）；标签小且淡；卡片圆角+阴影
    - 引擎 content-box（勿设 border-box）；禁止 table/emoji；用 flex 布局

    条形图示例::

        <div class="chart">
          <div class="crow"><span class="clab">美光</span>
            <div class="track"><div class="fill" style="width:55%;"></div></div>
            <span class="cval">-5.5%</span></div>
        </div>

    Args:
        ctx: 工具执行上下文
        html_content: body 片段（推荐）或完整 HTML
        image_format: 默认 png
        max_width: 默认 800

    Returns:
        文本确认，或 bot 不可用时的图片 bytes
    """
    if not html_content or not html_content.strip():
        return "渲染失败：HTML内容不能为空"

    try:
        html = _wrap_html_if_needed(html_content)
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

    需要自定义布局、指标卡、时间线时优先 ``render_html_to_image``。
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
