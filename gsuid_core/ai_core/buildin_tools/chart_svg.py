"""声明式图表原语（方案八）：chart_spec → 内联 SVG 片段。

渲染引擎（pytakumi）无 JS 执行能力，echarts/canvas 路线不可用——这是历史
出图「纯文字卡片化」的结构性原因。本模块给 render_agent 一组零依赖的 SVG
图表原语（line/bar/hbar/pie），模型只需声明数据结构，不必手写 SVG 几何。

用法：render_agent 先调 ``render_chart_spec`` 拿到 ``<svg>…</svg>`` 片段，
再把片段嵌进 HTML 一并交给 ``render_html_to_image``。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

_DEFAULT_W = 640
_DEFAULT_H = 360
_PAD = 40


def _num(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").replace("%", ""))
        except ValueError:
            return 0.0
    return 0.0


def _norm_series(data: Any) -> List[Dict[str, Any]]:
    """把 data 归一成 [{"label": str, "value": float}, ...]（容忍裸数值列表）。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(data, list):
        return out
    for i, item in enumerate(data):
        if isinstance(item, dict):
            label = str(item["label"]) if "label" in item else str(i + 1)
            out.append({"label": label, "value": _num(item["value"] if "value" in item else 0)})
        else:
            out.append({"label": str(i + 1), "value": _num(item)})
    return out


def _fmt(value: float) -> str:
    if abs(value) >= 10000:
        return f"{value / 10000:.1f}万"
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _axis_color(dark: bool) -> tuple[str, str]:
    return ("#94a3b8", "#334155") if dark else ("#64748b", "#cbd5e1")


def _svg_line(series: List[Dict[str, Any]], *, width: int, height: int, color: str, dark: bool) -> str:
    values = [p["value"] for p in series]
    lo, hi = min(values), max(values)
    if hi == lo:
        hi = lo + 1.0
    text_c, axis_c = _axis_color(dark)
    plot_w = width - _PAD * 2
    plot_h = height - _PAD * 2
    step = plot_w / max(1, len(series) - 1)

    def xy(i: int, v: float) -> tuple[float, float]:
        x = _PAD + i * step
        y = _PAD + plot_h - (v - lo) / (hi - lo) * plot_h
        return x, y

    pts = [xy(i, v) for i, v in enumerate(values)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(pts))
    area = f"{path} L{pts[-1][0]:.1f},{_PAD + plot_h:.1f} L{pts[0][0]:.1f},{_PAD + plot_h:.1f} Z"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" style="display:block">',
        f'<path d="{area}" fill="{color}" fill-opacity="0.12"/>',
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>',
    ]
    # 首尾点 + 数值标注；x 轴标签最多 8 个等距抽样
    for i, (x, y) in enumerate(pts):
        if i in (0, len(pts) - 1):
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"/>')
            anchor = "start" if i == 0 else "end"
            parts.append(
                f'<text x="{x:.1f}" y="{y - 8:.1f}" font-size="12" fill="{text_c}" '
                f'text-anchor="{anchor}">{_fmt(values[i])}</text>'
            )
    label_step = max(1, math.ceil(len(series) / 8))
    for i, p in enumerate(series):
        if i % label_step != 0 and i != len(series) - 1:
            continue
        x = _PAD + i * step
        parts.append(
            f'<text x="{x:.1f}" y="{height - 8:.1f}" font-size="11" fill="{text_c}" '
            f'text-anchor="middle">{p["label"][:10]}</text>'
        )
    parts.append(f'<line x1="{_PAD}" y1="{_PAD + plot_h}" x2="{width - _PAD}" y2="{_PAD + plot_h}" stroke="{axis_c}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_bar(
    series: List[Dict[str, Any]],
    *,
    width: int,
    height: int,
    color: str,
    dark: bool,
    horizontal: bool,
) -> str:
    text_c, axis_c = _axis_color(dark)
    values = [p["value"] for p in series]
    vmax = max(abs(v) for v in values) or 1.0
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" style="display:block">'
    ]
    n = len(series)
    if not horizontal:
        plot_w = width - _PAD * 2
        plot_h = height - _PAD * 2 - 14
        slot = plot_w / n
        bar_w = min(slot * 0.6, 56.0)
        for i, p in enumerate(series):
            h = abs(p["value"]) / vmax * plot_h
            x = _PAD + i * slot + (slot - bar_w) / 2
            y = _PAD + plot_h - h
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="3" fill="{color}"/>')
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" font-size="12" fill="{text_c}" '
                f'text-anchor="middle">{_fmt(p["value"])}</text>'
            )
            parts.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{height - 10:.1f}" font-size="11" fill="{text_c}" '
                f'text-anchor="middle">{p["label"][:8]}</text>'
            )
        parts.append(
            f'<line x1="{_PAD}" y1="{_PAD + plot_h}" x2="{width - _PAD}" y2="{_PAD + plot_h}" stroke="{axis_c}"/>'
        )
    else:
        label_w = 96.0
        plot_w = width - label_w - _PAD - 64
        slot = (height - _PAD) / n
        bar_h = min(slot * 0.6, 30.0)
        for i, p in enumerate(series):
            w = abs(p["value"]) / vmax * plot_w
            y = _PAD / 2 + i * slot + (slot - bar_h) / 2
            parts.append(
                f'<text x="{label_w - 8:.1f}" y="{y + bar_h / 2 + 4:.1f}" font-size="12" fill="{text_c}" '
                f'text-anchor="end">{p["label"][:8]}</text>'
            )
            parts.append(
                f'<rect x="{label_w:.1f}" y="{y:.1f}" width="{w:.1f}" height="{bar_h:.1f}" rx="3" fill="{color}"/>'
            )
            parts.append(
                f'<text x="{label_w + w + 6:.1f}" y="{y + bar_h / 2 + 4:.1f}" font-size="12" '
                f'fill="{text_c}">{_fmt(p["value"])}</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _svg_pie(series: List[Dict[str, Any]], *, width: int, height: int, colors: List[str], dark: bool) -> str:
    total = sum(max(0.0, p["value"]) for p in series)
    text_c, _ = _axis_color(dark)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" style="display:block">'
    ]
    if total <= 0:
        parts.append(
            f'<text x="{width / 2}" y="{height / 2}" font-size="13" fill="{text_c}">无有效占比数据</text></svg>'
        )
        return "".join(parts)
    cx = height / 2
    cy = height / 2
    r = height / 2 - 24
    angle = -math.pi / 2
    for i, p in enumerate(series):
        frac = max(0.0, p["value"]) / total
        if frac <= 0:
            continue
        a2 = angle + frac * 2 * math.pi
        large = 1 if frac > 0.5 else 0
        x1, y1 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        x2, y2 = cx + r * math.cos(a2), cy + r * math.sin(a2)
        fill = colors[i % len(colors)]
        parts.append(
            f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} A{r:.1f},{r:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z" '
            f'fill="{fill}"/>'
        )
        mid = (angle + a2) / 2
        lx, ly = cx + (r + 12) * math.cos(mid), cy + (r + 12) * math.sin(mid)
        if frac >= 0.05:
            anchor = "start" if math.cos(mid) >= 0 else "end"
            parts.append(
                f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="11" fill="{text_c}" '
                f'text-anchor="{anchor}">{p["label"][:8]} {frac * 100:.0f}%</text>'
            )
        angle = a2
    parts.append("</svg>")
    return "".join(parts)


_DEFAULT_COLORS = ["#38bdf8", "#f59e0b", "#34d399", "#f87171", "#a78bfa", "#facc15", "#4ade80", "#fb923c"]


def chart_spec_to_svg(spec: Dict[str, Any]) -> str:
    """chart_spec → 内联 SVG。错误时返回 ``⚠️`` 文案（工具层直接透传）。"""
    chart_type = str(spec["type"]).strip().lower() if "type" in spec else ""
    series = _norm_series(spec["data"] if "data" in spec else [])
    if not series:
        return "⚠️ chart_spec 缺少 data 数据点（需要 [{label, value}, ...] 或数值列表）"
    width = int(_num(spec["width"] if "width" in spec else _DEFAULT_W)) or _DEFAULT_W
    height = int(_num(spec["height"] if "height" in spec else _DEFAULT_H)) or _DEFAULT_H
    width = max(240, min(width, 1200))
    height = max(160, min(height, 900))
    color = str(spec["color"]) if "color" in spec and isinstance(spec["color"], str) else _DEFAULT_COLORS[0]
    dark = bool(spec["dark"]) if "dark" in spec else True
    if chart_type == "line":
        return _svg_line(series, width=width, height=height, color=color, dark=dark)
    if chart_type in ("bar", "hbar"):
        return _svg_bar(series, width=width, height=height, color=color, dark=dark, horizontal=chart_type == "hbar")
    if chart_type in ("pie", "donut"):
        return _svg_pie(series, width=width, height=height, colors=_DEFAULT_COLORS, dark=dark)
    return "⚠️ chart_spec.type 只支持 line / bar / hbar / pie"


@ai_tools(category="media", capability_domain="资料出图")
async def render_chart_spec(
    ctx: RunContext[ToolContext],
    type: str,
    data: List[Dict[str, Any]],
    width: int = _DEFAULT_W,
    height: int = _DEFAULT_H,
    color: str = "",
    dark: bool = True,
) -> str:
    """把声明式图表规格渲染成内联 SVG 片段（渲染引擎无 JS，echarts/canvas 不可用）。

    出图需要「图表」时的首选：拿到返回的 ``<svg>…</svg>`` 片段后，直接嵌进你的
    HTML（放在卡片容器里），再照常调 render_html_to_image——禁止再手写文字表格冒充图。

    类型选择：时间序列/走势 → line；类目对比/排行 → bar（项多时 hbar）；占比 → pie。

    Args:
        ctx: 工具执行上下文。
        type: line / bar / hbar / pie。
        data: 数据点列表，每项 {"label": "名称或时间", "value": 数值}；line 按给定顺序连线。
        width: SVG 宽度 px，默认 640。
        height: SVG 高度 px，默认 360。
        color: 主色（hex），默认天蓝；pie 自动多色。
        dark: 页面是否暗色主题（决定坐标轴/文字配色），默认 True。

    Returns:
        内联 ``<svg>…</svg>`` 片段（可直接拼入 HTML），失败时返回 ⚠️ 说明。
    """
    spec: Dict[str, Any] = {"type": type, "data": data, "width": width, "height": height, "dark": dark}
    if color:
        spec["color"] = color
    return chart_spec_to_svg(spec)
