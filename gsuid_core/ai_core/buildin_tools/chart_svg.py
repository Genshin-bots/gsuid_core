"""声明式图表原语：chart_spec → 内联 SVG 片段。

渲染引擎（pytakumi）无 JS，echarts/canvas 不可用。模型声明数据结构即可，
不必手写几何。多实体对比走 ``series`` 分组柱 + 图例，避免把身份拍扁进 label。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

_DEFAULT_W = 640
_DEFAULT_H = 360
_PAD = 40
_TITLE_H = 26
_LEGEND_H = 28
_MAX_LABEL = 18

# 系列身份色：刻意避开升/降语义的绿/红，避免「颜色=涨跌」和「颜色=系列」打架。
_SERIES_COLORS = ["#38bdf8", "#f59e0b", "#a78bfa", "#22d3ee", "#fb923c", "#c084fc", "#94a3b8", "#e879f9"]
_POS_DARK = "#34d399"
_NEG_DARK = "#f87171"
_POS_LIGHT = "#059669"
_NEG_LIGHT = "#dc2626"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _color_or_default(raw: str, fallback: str) -> str:
    s = raw.strip()
    hex_ok = len(s) in (4, 7) and s.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in s[1:])
    return s if hex_ok else fallback


def _num(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.replace(",", "").replace("%", "").strip()
        if s.startswith("+"):
            s = s[1:]
        if s.startswith("−"):
            s = "-" + s[1:]
        if not s:
            return 0.0
        neg = s.startswith("-")
        body = s[1:] if neg else s
        if body.count(".") > 1:
            return 0.0
        digits = body.replace(".", "")
        if not digits.isdigit():
            return 0.0
        n = float(body)
        return -n if neg else n
    return 0.0


def _clip_label(raw: str, max_n: int = _MAX_LABEL) -> str:
    text = raw.strip() if raw else ""
    if len(text) <= max_n:
        return _esc(text)
    return _esc(text[: max_n - 1] + "…")


def _unwrap_xml_array(data: object) -> object:
    """部分 provider 把 JSON 数组编成 ``{"item": [...]}``。"""
    cur: object = data
    for _ in range(2):
        if isinstance(cur, dict) and "item" in cur:
            inner = cur["item"]
            cur = inner if isinstance(inner, list) else [inner]
        else:
            break
    return cur


def _norm_points(data: object) -> List[Dict[str, Any]]:
    """把 data 归一成 [{"label": str, "value": float}, ...]。"""
    data = _unwrap_xml_array(data)
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


def _sign_colors(dark: bool) -> tuple[str, str]:
    return (_POS_DARK, _NEG_DARK) if dark else (_POS_LIGHT, _NEG_LIGHT)


def _bar_fill(*, value: float, series_color: str, signed: bool, dark: bool) -> str:
    if not signed:
        return series_color
    pos, neg = _sign_colors(dark)
    return pos if value >= 0 else neg


def _parse_named_series(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """归一成 [{name, color, points}, ...]。兼容旧 data 单系列。"""
    named: List[Dict[str, Any]] = []
    raw_series = _unwrap_xml_array(spec["series"] if "series" in spec else None)
    if isinstance(raw_series, list) and raw_series:
        for i, item in enumerate(raw_series):
            if not isinstance(item, dict):
                continue
            pts = _norm_points(item["data"] if "data" in item else [])
            if not pts:
                continue
            name = str(item["name"]) if "name" in item and str(item["name"]).strip() else f"系列{i + 1}"
            fallback = _SERIES_COLORS[i % len(_SERIES_COLORS)]
            color = (
                _color_or_default(str(item["color"]), fallback)
                if "color" in item and isinstance(item["color"], str) and item["color"]
                else fallback
            )
            named.append({"name": name, "color": color, "points": pts})
        if named:
            return named
    points = _norm_points(spec["data"] if "data" in spec else [])
    if not points:
        return []
    has_color = "color" in spec and isinstance(spec["color"], str) and spec["color"]
    color = _color_or_default(str(spec["color"]), _SERIES_COLORS[0]) if has_color else _SERIES_COLORS[0]
    return [{"name": "", "color": color, "points": points}]


def _union_labels(named: Sequence[Dict[str, Any]]) -> List[str]:
    """类目轴：以最长系列的顺序为骨架，再追加其它系列多出来的 label。"""
    best: List[str] = []
    for ser in named:
        pts: List[Dict[str, Any]] = ser["points"]
        labs = [str(p["label"]) for p in pts]
        if len(labs) > len(best):
            best = labs
    seen: set[str] = set(best)
    out = list(best)
    for ser in named:
        pts = ser["points"]
        for p in pts:
            lab = str(p["label"])
            if lab not in seen:
                seen.add(lab)
                out.append(lab)
    return out


def _value_map(points: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for p in points:
        out[str(p["label"])] = float(p["value"])
    return out


def _header_offset(*, title: str, legend: bool) -> int:
    extra = 0
    if title:
        extra += _TITLE_H
    if legend:
        extra += _LEGEND_H
    return extra


def _svg_open(width: int, height: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" style="display:block">'
    )


def _draw_title_legend(
    parts: List[str],
    *,
    width: int,
    title: str,
    named: Sequence[Dict[str, Any]],
    legend: bool,
    signed: bool,
    dark: bool,
    text_c: str,
) -> int:
    y = 18
    if title:
        parts.append(
            f'<text x="{width / 2:.1f}" y="{y}" font-size="14" font-weight="630" '
            f'fill="{text_c}" text-anchor="middle">{_clip_label(title, 40)}</text>'
        )
        y += _TITLE_H - 4
    if legend and len(named) >= 2:
        x = _PAD
        for ser in named:
            if signed:
                parts.append(
                    f'<text x="{x:.1f}" y="{y}" font-size="12" fill="{text_c}">'
                    f"{_clip_label(str(ser['name']), 12)}</text>"
                )
                x += 12 * min(len(str(ser["name"])), 12) + 16
            else:
                parts.append(
                    f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="10" height="10" rx="2" fill="{ser["color"]}"/>'
                )
                parts.append(
                    f'<text x="{x + 14:.1f}" y="{y}" font-size="12" fill="{text_c}">'
                    f"{_clip_label(str(ser['name']), 12)}</text>"
                )
                x += 14 + 12 * min(len(str(ser["name"])), 12) + 18
        if signed:
            pos, neg = _sign_colors(dark)
            parts.append(f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="10" height="10" rx="2" fill="{pos}"/>')
            parts.append(f'<text x="{x + 14:.1f}" y="{y}" font-size="11" fill="{text_c}">+</text>')
            x += 36
            parts.append(f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="10" height="10" rx="2" fill="{neg}"/>')
            parts.append(f'<text x="{x + 14:.1f}" y="{y}" font-size="11" fill="{text_c}">−</text>')
        y += _LEGEND_H - 8
    return y


def _svg_line(
    named: Sequence[Dict[str, Any]],
    *,
    width: int,
    height: int,
    dark: bool,
    title: str = "",
    legend: bool = True,
) -> str:
    labels = _union_labels(named)
    maps = [_value_map(ser["points"]) for ser in named]
    values = [m[lab] for m in maps for lab in labels if lab in m]
    if not values:
        return "⚠️ chart_spec 缺少 data / series 数据点（需要 [{label, value}, ...] 或 series[]）"
    lo, hi = min(values), max(values)
    if lo < 0:
        hi = max(hi, 0.0)
    if hi == lo:
        hi = lo + 1.0
    text_c, axis_c = _axis_color(dark)
    show_legend = legend and len(named) >= 2
    head = _header_offset(title=title, legend=show_legend)
    plot_w = width - _PAD * 2
    plot_h = height - _PAD * 2 - head
    top = _PAD + head
    n = max(1, len(labels) - 1)
    step = plot_w / n
    parts = [_svg_open(width, height)]
    _draw_title_legend(
        parts,
        width=width,
        title=title,
        named=named,
        legend=show_legend,
        signed=False,
        dark=dark,
        text_c=text_c,
    )

    def xy(i: int, v: float) -> tuple[float, float]:
        x = _PAD + i * step
        y = top + plot_h - (v - lo) / (hi - lo) * plot_h
        return x, y

    for ser, mp in zip(named, maps):
        color = str(ser["color"])
        segs: list[list[tuple[int, float, float]]] = []
        cur: list[tuple[int, float, float]] = []
        for i, lab in enumerate(labels):
            if lab not in mp:
                if cur:
                    segs.append(cur)
                    cur = []
                continue
            x, y = xy(i, mp[lab])
            cur.append((i, x, y))
        if cur:
            segs.append(cur)
        for pts in segs:
            if len(pts) == 1:
                _, x, y = pts[0]
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"/>')
                continue
            path = " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{y:.1f}" for j, (_, x, y) in enumerate(pts))
            if len(named) == 1 and len(segs) == 1:
                area = f"{path} L{pts[-1][1]:.1f},{top + plot_h:.1f} L{pts[0][1]:.1f},{top + plot_h:.1f} Z"
                parts.append(f'<path d="{area}" fill="{color}" fill-opacity="0.12"/>')
            parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>')
            for j, (idx, x, y) in enumerate(pts):
                if j not in (0, len(pts) - 1):
                    continue
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"/>')
                if len(named) == 1:
                    lab_i = labels[idx]
                    v = mp[lab_i]
                    anchor = "start" if j == 0 else "end"
                    parts.append(
                        f'<text x="{x:.1f}" y="{y - 8:.1f}" font-size="12" fill="{text_c}" '
                        f'text-anchor="{anchor}">{_fmt(v)}</text>'
                    )
    label_step = max(1, math.ceil(len(labels) / 8))
    for i, lab in enumerate(labels):
        if i % label_step != 0 and i != len(labels) - 1:
            continue
        x = _PAD + i * step
        parts.append(
            f'<text x="{x:.1f}" y="{height - 8:.1f}" font-size="11" fill="{text_c}" '
            f'text-anchor="middle">{_clip_label(lab)}</text>'
        )
    parts.append(f'<line x1="{_PAD}" y1="{top + plot_h}" x2="{width - _PAD}" y2="{top + plot_h}" stroke="{axis_c}"/>')
    if lo < 0:
        zero_y = top + plot_h - (0.0 - lo) / (hi - lo) * plot_h
        parts.append(
            f'<line x1="{_PAD}" y1="{zero_y:.1f}" x2="{width - _PAD}" y2="{zero_y:.1f}" '
            f'stroke="{axis_c}" stroke-dasharray="4 3"/>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _svg_grouped_bar(
    named: Sequence[Dict[str, Any]],
    *,
    width: int,
    height: int,
    dark: bool,
    horizontal: bool,
    signed: bool,
    title: str,
    legend: bool,
) -> str:
    labels = _union_labels(named)
    if not labels:
        return "⚠️ chart_spec 缺少类目 label"
    maps = [_value_map(ser["points"]) for ser in named]
    all_vals = [m[lab] for m in maps for lab in labels if lab in m]
    vmax = max((abs(v) for v in all_vals), default=1.0) or 1.0
    bipolar = bool(signed or any(v < 0 for v in all_vals))
    text_c, axis_c = _axis_color(dark)
    n_cat = len(labels)
    n_ser = len(named)
    show_legend = legend and n_ser >= 2
    head = _header_offset(title=title, legend=show_legend)
    parts = [_svg_open(width, height)]
    _draw_title_legend(
        parts,
        width=width,
        title=title,
        named=named,
        legend=show_legend,
        signed=signed,
        dark=dark,
        text_c=text_c,
    )
    if not horizontal:
        plot_w = width - _PAD * 2
        plot_h = height - _PAD * 2 - 16 - head
        top = _PAD + head
        zero_y = top + plot_h / 2.0 if bipolar else top + plot_h
        span = plot_h / 2.0 if bipolar else plot_h
        slot = plot_w / n_cat
        bar_w = min(slot * 0.72 / n_ser, 36.0)
        group_w = bar_w * n_ser
        for ci, lab in enumerate(labels):
            gx = _PAD + ci * slot + (slot - group_w) / 2
            for si, ser in enumerate(named):
                if lab not in maps[si]:
                    continue
                val = maps[si][lab]
                h = abs(val) / vmax * span
                x = gx + si * bar_w
                y = zero_y - h if val >= 0 else zero_y
                fill = _bar_fill(value=val, series_color=str(ser["color"]), signed=signed, dark=dark)
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="2" fill="{fill}"/>'
                )
                if n_cat * n_ser <= 16:
                    ty = y - 4 if val >= 0 else y + h + 12
                    parts.append(
                        f'<text x="{x + bar_w / 2:.1f}" y="{ty:.1f}" font-size="10" fill="{text_c}" '
                        f'text-anchor="middle">{_fmt(val)}</text>'
                    )
            parts.append(
                f'<text x="{_PAD + ci * slot + slot / 2:.1f}" y="{height - 8:.1f}" font-size="11" fill="{text_c}" '
                f'text-anchor="middle">{_clip_label(lab)}</text>'
            )
        parts.append(f'<line x1="{_PAD}" y1="{zero_y:.1f}" x2="{width - _PAD}" y2="{zero_y:.1f}" stroke="{axis_c}"/>')
    else:
        max_lab = max((len(lab) for lab in labels), default=4)
        label_w = float(min(160, max(72, max_lab * 12 + 8)))
        plot_w = width - label_w - _PAD - 64
        plot_h = height - _PAD - head
        top = head + 8
        zero_x = label_w + plot_w / 2.0 if bipolar else label_w
        span = plot_w / 2.0 if bipolar else plot_w
        slot = plot_h / n_cat
        bar_h = min(slot * 0.72 / n_ser, 18.0)
        for ci, lab in enumerate(labels):
            gy = top + ci * slot + (slot - bar_h * n_ser) / 2
            parts.append(
                f'<text x="{label_w - 8:.1f}" y="{gy + (bar_h * n_ser) / 2 + 4:.1f}" font-size="12" fill="{text_c}" '
                f'text-anchor="end">{_clip_label(lab)}</text>'
            )
            for si, ser in enumerate(named):
                if lab not in maps[si]:
                    continue
                val = maps[si][lab]
                w = abs(val) / vmax * span
                y = gy + si * bar_h
                x = zero_x if val >= 0 else zero_x - w
                fill = _bar_fill(value=val, series_color=str(ser["color"]), signed=signed, dark=dark)
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{bar_h:.1f}" rx="2" fill="{fill}"/>'
                )
                tx = x + w + 6 if val >= 0 else x - 6
                anchor = "start" if val >= 0 else "end"
                parts.append(
                    f'<text x="{tx:.1f}" y="{y + bar_h / 2 + 4:.1f}" font-size="11" fill="{text_c}" '
                    f'text-anchor="{anchor}">{_fmt(val)}</text>'
                )
        if bipolar:
            parts.append(
                f'<line x1="{zero_x:.1f}" y1="{top}" x2="{zero_x:.1f}" y2="{top + plot_h}" stroke="{axis_c}"/>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _svg_pie(
    series: List[Dict[str, Any]],
    *,
    width: int,
    height: int,
    colors: List[str],
    dark: bool,
    title: str,
) -> str:
    total = sum(max(0.0, p["value"]) for p in series)
    text_c, _ = _axis_color(dark)
    parts = [_svg_open(width, height)]
    _draw_title_legend(parts, width=width, title=title, named=[], legend=False, signed=False, dark=dark, text_c=text_c)
    if total <= 0:
        parts.append(
            f'<text x="{width / 2}" y="{height / 2}" font-size="13" fill="{text_c}">无有效占比数据</text></svg>'
        )
        return "".join(parts)
    head = _header_offset(title=title, legend=False)
    cx = width / 2 - 40
    cy = (height + head) / 2
    r = min(cx, height - head) / 2 - 24
    angle = -math.pi / 2
    lx0 = width - 16
    ly = head + 28
    for i, p in enumerate(series):
        frac = max(0.0, p["value"]) / total
        fill = colors[i % len(colors)]
        if frac > 0:
            a2 = angle + frac * 2 * math.pi
            large = 1 if frac > 0.5 else 0
            x1, y1 = cx + r * math.cos(angle), cy + r * math.sin(angle)
            x2, y2 = cx + r * math.cos(a2), cy + r * math.sin(a2)
            parts.append(
                f'<path d="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} A{r:.1f},{r:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z" '
                f'fill="{fill}"/>'
            )
            angle = a2
        parts.append(f'<rect x="{lx0 - 118:.1f}" y="{ly - 10:.1f}" width="10" height="10" rx="2" fill="{fill}"/>')
        parts.append(
            f'<text x="{lx0 - 104:.1f}" y="{ly}" font-size="11" fill="{text_c}">'
            f"{_clip_label(str(p['label']))} {frac * 100:.0f}%</text>"
        )
        ly += 18
    parts.append("</svg>")
    return "".join(parts)


def chart_spec_to_svg(spec: Dict[str, Any]) -> str:
    """chart_spec → 内联 SVG。错误时返回 ``⚠️`` 文案（工具层直接透传）。"""
    chart_type = str(spec["type"]).strip().lower() if "type" in spec else ""
    named = _parse_named_series(spec)
    if not named:
        return "⚠️ chart_spec 缺少 data / series 数据点（需要 [{label, value}, ...] 或 series[]）"
    width = int(_num(spec["width"] if "width" in spec else _DEFAULT_W)) or _DEFAULT_W
    height = int(_num(spec["height"] if "height" in spec else _DEFAULT_H)) or _DEFAULT_H
    width = max(240, min(width, 1200))
    height = max(160, min(height, 900))
    dark = bool(spec["dark"]) if "dark" in spec else True
    signed = bool(spec["signed"]) if "signed" in spec else False
    title = str(spec["title"]).strip() if "title" in spec and spec["title"] else ""
    legend = bool(spec["legend"]) if "legend" in spec else True
    if chart_type == "line":
        return _svg_line(named, width=width, height=height, dark=dark, title=title, legend=legend)
    if chart_type in ("bar", "hbar"):
        return _svg_grouped_bar(
            named,
            width=width,
            height=height,
            dark=dark,
            horizontal=chart_type == "hbar",
            signed=signed,
            title=title,
            legend=legend,
        )
    if chart_type in ("pie", "donut"):
        return _svg_pie(named[0]["points"], width=width, height=height, colors=_SERIES_COLORS, dark=dark, title=title)
    return "⚠️ chart_spec.type 只支持 line / bar / hbar / pie"


@ai_tools(category="media", capability_domain="资料出图")
async def render_chart_spec(
    ctx: RunContext[ToolContext],
    type: str,
    data: List[Dict[str, Any]] | None = None,
    series: List[Dict[str, Any]] | None = None,
    width: int = _DEFAULT_W,
    height: int = _DEFAULT_H,
    color: str = "",
    dark: bool = True,
    signed: bool = False,
    title: str = "",
    legend: bool = True,
) -> str:
    """把声明式图表规格渲染成内联 SVG 片段（渲染引擎无 JS）。

    多实体 × 多指标对比必须传 ``series``（每实体一个 ``name``），不要把身份写进
    单根柱的 label。有正负含义的值开 ``signed``：升/降色只表达符号，系列靠分组位置
    和图例区分。

    类型：时间序列 → line；类目对比 → bar（项多时 hbar）；占比 → pie。

    Args:
        ctx: 工具执行上下文。
        type: line / bar / hbar / pie。
        data: 单系列数据点 [{label, value}, ...]；与 series 二选一，series 优先。
        series: 多系列 [{name, data: [{label, value}], color?}, ...]。
        width: SVG 宽度 px，默认 640。
        height: SVG 高度 px，默认 360。
        color: 单系列主色；多系列忽略，用内置身份色（不含升/降红绿）。
        dark: 暗色主题（轴/字色），默认 True。
        signed: True 时柱色按正负取升/降色，系列不靠红绿区分。
        title: 可选图题。
        legend: 多系列时画图例，默认 True。

    Returns:
        内联 ``<svg>…</svg>`` 片段，失败时返回 ⚠️ 说明。
    """
    _ = ctx
    spec: Dict[str, Any] = {
        "type": type,
        "width": width,
        "height": height,
        "dark": dark,
        "signed": signed,
        "legend": legend,
    }
    if title:
        spec["title"] = title
    if series:
        spec["series"] = series
    elif data:
        spec["data"] = data
    if color:
        spec["color"] = color
    return chart_spec_to_svg(spec)
