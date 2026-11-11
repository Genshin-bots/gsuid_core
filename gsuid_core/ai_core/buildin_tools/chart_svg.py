"""声明式图表原语：chart_spec → 内联 SVG 片段。

渲染引擎（pytakumi）无 JS，echarts/canvas 不可用。模型声明数据结构即可，
不必手写几何。支持折线、正负比例柱、横向胶囊对比条、非零锚定关键位阶梯与环形图。
"""

from __future__ import annotations

import re
import math
import hashlib
from typing import Mapping, Sequence
from dataclasses import dataclass

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools

ChartScalar = str | int | float | bool | None
ChartPointInput = dict[str, ChartScalar]
ChartSeriesInput = dict[str, ChartScalar | list[ChartPointInput]]
ChartPointMap = Mapping[str, ChartScalar]
ChartSeriesMap = Mapping[str, ChartScalar | Sequence[ChartPointMap]]
ChartSpecValue = ChartScalar | Sequence[ChartPointMap] | Sequence[ChartSeriesMap] | Sequence[ChartScalar]

_DEFAULT_W = 720
_DEFAULT_H = 380
_PAD = 42
_TITLE_H = 32
_LEGEND_H = 30
_MAX_LABEL = 18

# 系列身份色：避开升/降语义红绿，保证多系列对比清晰。
_SERIES_COLORS = [
    "#38bdf8",
    "#f59e0b",
    "#a78bfa",
    "#2dd4bf",
    "#fb923c",
    "#818cf8",
    "#f472b6",
    "#94a3b8",
]
_BENCH_PEERS_DARK = ["#14b8a6", "#fb923c", "#6366f1", "#a855f7", "#64748b"]
_BENCH_PEERS_LIGHT = ["#0d9488", "#ea580c", "#4f46e5", "#9333ea", "#64748b"]
_POS_DARK = "#fb7185"
_NEG_DARK = "#34d399"
_POS_LIGHT = "#dc2626"
_NEG_LIGHT = "#059669"
_CHART_CACHE_MAX = 128
_RENDERED_CHART_SVGS: dict[str, str] = {}
_CHART_TOKEN_RE = re.compile(
    r"\{\{\s*(chart_[0-9a-f]{8})\s*\}\}"
    r"|__(chart_[0-9a-f]{8})__"
    r"|<div\b[^>]*\bdata-chart=['\"](chart_[0-9a-f]{8})['\"][^>]*>\s*</div>",
    re.IGNORECASE,
)


def register_chart_svg(svg: str) -> str:
    cid = "chart_" + hashlib.sha1(svg.encode("utf-8")).hexdigest()[:8]
    if len(_RENDERED_CHART_SVGS) >= _CHART_CACHE_MAX and cid not in _RENDERED_CHART_SVGS:
        _RENDERED_CHART_SVGS.pop(next(iter(_RENDERED_CHART_SVGS)))
    _RENDERED_CHART_SVGS[cid] = svg
    return cid


def resolve_chart_placeholders(html: str) -> str:
    if not html or "chart_" not in html.lower():
        return html

    def _sub(m: re.Match[str]) -> str:
        cid = (m.group(1) or m.group(2) or m.group(3) or "").lower()
        if cid in _RENDERED_CHART_SVGS:
            return _RENDERED_CHART_SVGS[cid]
        return m.group(0)

    return _CHART_TOKEN_RE.sub(_sub, html)


@dataclass(slots=True)
class _Point:
    label: str
    value: float
    sub: str = ""
    unit: str = ""
    color: str = ""
    role: str = ""
    highlight: bool = False


@dataclass(slots=True)
class _NamedSeries:
    name: str
    color: str
    points: list[_Point]


@dataclass(slots=True)
class _Palette:
    title: str
    val: str
    text: str
    muted: str
    axis: str
    grid: str
    track: str
    bg_stroke: str
    accent: str


def _palette(dark: bool) -> _Palette:
    if dark:
        return _Palette(
            title="#f8fafc",
            val="#f1f5f9",
            text="#cbd5e1",
            muted="#94a3b8",
            axis="#475569",
            grid="#1e293b",
            track="#1e293b",
            bg_stroke="#0f172a",
            accent="#22d3ee",
        )
    return _Palette(
        title="#0f172a",
        val="#0f172a",
        text="#334155",
        muted="#64748b",
        axis="#cbd5e1",
        grid="#e2e8f0",
        track="#f1f5f9",
        bg_stroke="#ffffff",
        accent="#0284c7",
    )


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _color_or_default(raw: str, fallback: str) -> str:
    s = raw.strip()
    hex_ok = len(s) in (4, 7) and s.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in s[1:])
    return s if hex_ok else fallback


def _parse_num_unit(value: object) -> tuple[float, str]:
    if isinstance(value, bool):
        return (1.0 if value else 0.0), ""
    if isinstance(value, (int, float)):
        return float(value), ""
    if isinstance(value, str):
        s = value.replace(",", "").strip()
        if not s:
            return 0.0, ""
        unit = ""
        while s and s[-1] not in "0123456789.":
            unit = s[-1] + unit
            s = s[:-1].strip()
        if s.startswith("+"):
            s = s[1:]
        elif s.startswith("−"):
            s = "-" + s[1:]
        if not s:
            return 0.0, unit[:4]
        neg = s.startswith("-")
        body = s[1:] if neg else s
        if body.count(".") > 1:
            return 0.0, ""
        digits = body.replace(".", "")
        if not digits.isdigit():
            return 0.0, ""
        n = float(body)
        return (-n if neg else n), unit[:4]
    return 0.0, ""


def _num(value: object) -> float:
    n, _ = _parse_num_unit(value)
    return n


def _clip_label(raw: str, max_n: int = _MAX_LABEL) -> str:
    text = raw.strip() if raw else ""
    if len(text) <= max_n:
        return _esc(text)
    return _esc(text[: max_n - 1] + "…")


def _unwrap_xml_array(data: object) -> object:
    """部分 provider 把 JSON 数组编成 ``{"item": [...]}``。"""
    cur: object = data
    for _ in range(2):
        if isinstance(cur, Mapping) and "item" in cur:
            inner = cur["item"]
            cur = inner if isinstance(inner, list) else [inner]
        else:
            break
    return cur


def _norm_points(data: object, default_unit: str = "") -> list[_Point]:
    data = _unwrap_xml_array(data)
    out: list[_Point] = []
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
        return out
    for i, item in enumerate(data):
        if isinstance(item, Mapping):
            label = str(item["label"]) if "label" in item and item["label"] is not None else str(i + 1)
            val_raw = item["value"] if "value" in item else 0
            val, inferred_unit = _parse_num_unit(val_raw)
            pt_unit = (
                str(item["unit"]).strip()
                if "unit" in item and item["unit"] is not None and str(item["unit"]).strip()
                else (default_unit or inferred_unit)
            )
            sub = str(item["sub"]).strip() if "sub" in item and item["sub"] is not None else ""
            raw_color = str(item["color"]).strip() if "color" in item and isinstance(item["color"], str) else ""
            role = str(item["role"]).strip().lower() if "role" in item and item["role"] is not None else ""
            hl = (
                bool(item["highlight"])
                if "highlight" in item
                else role in ("current", "winner", "highlight", "primary")
            )
            out.append(
                _Point(
                    label=label,
                    value=val,
                    sub=sub,
                    unit=pt_unit,
                    color=_color_or_default(raw_color, "") if raw_color else "",
                    role=role,
                    highlight=hl,
                )
            )
        else:
            val, inferred_unit = _parse_num_unit(item)
            out.append(_Point(label=str(i + 1), value=val, unit=default_unit or inferred_unit))
    return out


def _fmt(value: float, *, unit: str = "", signed: bool = False) -> str:
    prefix = "+" if signed and value > 0 else ""
    if abs(value) >= 10000 and not unit:
        return f"{prefix}{value / 10000:.1f}万"
    if value == int(value):
        body = str(int(value))
    else:
        body = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{prefix}{body}{unit}"


def _sign_colors(dark: bool) -> tuple[str, str]:
    return (_POS_DARK, _NEG_DARK) if dark else (_POS_LIGHT, _NEG_LIGHT)


def _bar_fill(*, value: float, series_color: str, signed: bool, dark: bool) -> str:
    if not signed:
        return series_color
    pos, neg = _sign_colors(dark)
    return pos if value >= 0 else neg


def _parse_named_series(spec: Mapping[str, ChartSpecValue], default_unit: str) -> list[_NamedSeries]:
    named: list[_NamedSeries] = []
    raw_series = _unwrap_xml_array(spec["series"] if "series" in spec else None)
    if isinstance(raw_series, Sequence) and not isinstance(raw_series, (str, bytes)) and raw_series:
        for i, item in enumerate(raw_series):
            if not isinstance(item, Mapping):
                continue
            pts = _norm_points(item["data"] if "data" in item else [], default_unit)
            if not pts:
                continue
            name = (
                str(item["name"]).strip()
                if "name" in item and item["name"] is not None and str(item["name"]).strip()
                else f"系列{i + 1}"
            )
            fallback = _SERIES_COLORS[i % len(_SERIES_COLORS)]
            color = (
                _color_or_default(str(item["color"]), fallback)
                if "color" in item and isinstance(item["color"], str) and item["color"]
                else fallback
            )
            named.append(_NamedSeries(name=name, color=color, points=pts))
        if named:
            return named
    points = _norm_points(spec["data"] if "data" in spec else [], default_unit)
    if not points:
        return []
    has_color = "color" in spec and isinstance(spec["color"], str) and bool(spec["color"])
    color = _color_or_default(str(spec["color"]), _SERIES_COLORS[0]) if has_color else _SERIES_COLORS[0]
    return [_NamedSeries(name="", color=color, points=points)]


def _union_labels(named: Sequence[_NamedSeries]) -> list[str]:
    best: list[str] = []
    for ser in named:
        labs = [p.label for p in ser.points]
        if len(labs) > len(best):
            best = labs
    seen: set[str] = set(best)
    out = list(best)
    for ser in named:
        for p in ser.points:
            if p.label not in seen:
                seen.add(p.label)
                out.append(p.label)
    return out


def _point_map(points: Sequence[_Point]) -> dict[str, _Point]:
    return {p.label: p for p in points}


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
    parts: list[str],
    *,
    width: int,
    title: str,
    named: Sequence[_NamedSeries],
    legend: bool,
    signed: bool,
    dark: bool,
    pal: _Palette,
) -> int:
    y = 24
    if title:
        parts.append(
            f'<text x="{_PAD:.1f}" y="{y}" font-size="16" font-weight="700" '
            f'fill="{pal.title}" text-anchor="start">{_clip_label(title, 42)}</text>'
        )
        y += _TITLE_H - 4
    if legend and len(named) >= 2:
        x = float(_PAD)
        for ser in named:
            if signed:
                parts.append(
                    f'<text x="{x:.1f}" y="{y}" font-size="12.5" font-weight="600" fill="{pal.text}">'
                    f"{_clip_label(ser.name, 14)}</text>"
                )
                x += 13 * min(len(ser.name), 14) + 18
            else:
                parts.append(f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="12" height="12" rx="3" fill="{ser.color}"/>')
                parts.append(
                    f'<text x="{x + 17:.1f}" y="{y}" font-size="12.5" font-weight="500" fill="{pal.text}">'
                    f"{_clip_label(ser.name, 14)}</text>"
                )
                x += 17 + 13 * min(len(ser.name), 14) + 20
        if signed:
            pos, neg = _sign_colors(dark)
            parts.append(f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="11" height="11" rx="3" fill="{pos}"/>')
            parts.append(
                f'<text x="{x + 15:.1f}" y="{y}" font-size="12" font-weight="600" fill="{pal.text}">正值</text>'
            )
            x += 52
            parts.append(f'<rect x="{x:.1f}" y="{y - 10:.1f}" width="11" height="11" rx="3" fill="{neg}"/>')
            parts.append(
                f'<text x="{x + 15:.1f}" y="{y}" font-size="12" font-weight="600" fill="{pal.text}">负值</text>'
            )
        y += _LEGEND_H - 6
    return y


def _svg_line(
    named: Sequence[_NamedSeries],
    *,
    width: int,
    height: int,
    dark: bool,
    title: str = "",
    legend: bool = True,
    unit: str = "",
) -> str:
    labels = _union_labels(named)
    maps = [_point_map(ser.points) for ser in named]
    pts_all = [m[lab] for m in maps for lab in labels if lab in m]
    if not pts_all:
        return "⚠️ chart_spec 缺少 data / series 数据点（需要 [{label, value}, ...] 或 series[]）"
    raw_lo = min(p.value for p in pts_all)
    raw_hi = max(p.value for p in pts_all)
    span = raw_hi - raw_lo
    if span == 0:
        span = abs(raw_hi) * 0.2 or 1.0
    lo = raw_lo - span * 0.12
    hi = raw_hi + span * 0.16
    if raw_lo >= 0 and lo < 0:
        lo = 0.0
    pal = _palette(dark)
    show_legend = legend and len(named) >= 2
    head = _header_offset(title=title, legend=show_legend)
    pad_l = 62
    pad_r = 34
    plot_w = max(120, width - pad_l - pad_r)
    plot_h = max(90, height - head - 62)
    top = head + 18
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
        pal=pal,
    )
    sample_unit = unit or pts_all[0].unit
    for frac in (0.0, 0.5, 1.0):
        gy = top + plot_h * (1.0 - frac)
        gv = lo + (hi - lo) * frac
        parts.append(
            f'<line x1="{pad_l}" y1="{gy:.1f}" x2="{width - pad_r}" y2="{gy:.1f}" '
            f'stroke="{pal.grid}" stroke-width="1" stroke-dasharray="4 4"/>'
        )
        parts.append(
            f'<text x="{pad_l - 8:.1f}" y="{gy + 4:.1f}" font-size="11.5" fill="{pal.muted}" '
            f'text-anchor="end">{_esc(_fmt(gv, unit=sample_unit))}</text>'
        )

    def xy(i: int, v: float) -> tuple[float, float]:
        x = pad_l + i * step
        y = top + plot_h - (v - lo) / (hi - lo) * plot_h
        return x, y

    if lo < 0 < hi:
        zero_y = top + plot_h - (0.0 - lo) / (hi - lo) * plot_h
        parts.append(
            f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{width - pad_r}" y2="{zero_y:.1f}" '
            f'stroke="{pal.axis}" stroke-width="1.2" stroke-dasharray="5 3"/>'
        )

    for ser, mp in zip(named, maps):
        color = ser.color
        segs: list[list[tuple[int, float, float, _Point]]] = []
        cur: list[tuple[int, float, float, _Point]] = []
        for i, lab in enumerate(labels):
            if lab not in mp:
                if cur:
                    segs.append(cur)
                    cur = []
                continue
            pt = mp[lab]
            x, y = xy(i, pt.value)
            cur.append((i, x, y, pt))
        if cur:
            segs.append(cur)
        for pts in segs:
            if len(pts) == 1:
                _, x, y, _ = pts[0]
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{color}"/>')
                continue
            path = " ".join(f"{'M' if j == 0 else 'L'}{x:.1f},{y:.1f}" for j, (_, x, y, _) in enumerate(pts))
            if len(named) == 1 and len(segs) == 1:
                area = f"{path} L{pts[-1][1]:.1f},{top + plot_h:.1f} L{pts[0][1]:.1f},{top + plot_h:.1f} Z"
                parts.append(f'<path d="{area}" fill="{color}" fill-opacity="0.14"/>')
            parts.append(
                f'<path d="{path}" fill="none" stroke="{color}" stroke-width="3" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
            )
            max_j = max(range(len(pts)), key=lambda idx_j: pts[idx_j][3].value)
            min_j = min(range(len(pts)), key=lambda idx_j: pts[idx_j][3].value)
            key_js = set(range(len(pts))) if len(pts) <= 7 else {0, len(pts) - 1, max_j, min_j}
            for j, (_, x, y, pt) in enumerate(pts):
                if j not in key_js:
                    continue
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="{pal.bg_stroke}" '
                    f'stroke="{color}" stroke-width="2.5"/>'
                )
                if len(named) == 1:
                    anchor = "start" if j == 0 else ("end" if j == len(pts) - 1 else "middle")
                    dy = -10.0 if j != min_j or len(pts) <= 3 else 18.0
                    parts.append(
                        f'<text x="{x:.1f}" y="{y + dy:.1f}" font-size="12.5" font-weight="700" '
                        f'fill="{pal.val}" text-anchor="{anchor}">{_esc(_fmt(pt.value, unit=pt.unit))}</text>'
                    )

    label_step = max(1, math.ceil(len(labels) / 8))
    for i, lab in enumerate(labels):
        if i % label_step != 0 and i != len(labels) - 1:
            continue
        x = pad_l + i * step
        parts.append(
            f'<text x="{x:.1f}" y="{height - 12:.1f}" font-size="12.5" font-weight="500" '
            f'fill="{pal.text}" text-anchor="middle">{_clip_label(lab, 12)}</text>'
        )
    parts.append(
        f'<line x1="{pad_l}" y1="{top + plot_h:.1f}" x2="{width - pad_r}" y2="{top + plot_h:.1f}" '
        f'stroke="{pal.axis}" stroke-width="1.2"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _est_text_px(text: str, font_size: float = 12.5) -> float:
    return sum(font_size * (1.02 if ord(ch) > 127 else 0.64) for ch in text)


def _svg_grouped_bar(
    named: Sequence[_NamedSeries],
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
    maps = [_point_map(ser.points) for ser in named]
    pts_all = [m[lab] for m in maps for lab in labels if lab in m]
    all_vals = [p.value for p in pts_all]
    pos_max = max([0.0] + [v for v in all_vals if v > 0])
    neg_max = max([0.0] + [-v for v in all_vals if v < 0])
    bipolar = neg_max > 0 and pos_max > 0
    only_neg = neg_max > 0 and pos_max == 0
    pal = _palette(dark)
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
        pal=pal,
    )

    if not horizontal:
        plot_w = width - _PAD * 2
        top_pad = 26
        bot_pad = 42 if (bipolar or only_neg) else 34
        usable_h = max(80.0, float(height - head - top_pad - bot_pad))
        top = float(head + top_pad)
        if bipolar:
            pos_ratio = max(0.22, min(0.78, pos_max / (pos_max + neg_max)))
            pos_span = usable_h * pos_ratio
            neg_span = usable_h - pos_span
            zero_y = top + pos_span
        elif only_neg:
            pos_span = 0.0
            neg_span = usable_h
            zero_y = top
        else:
            pos_span = usable_h
            neg_span = 0.0
            zero_y = top + usable_h

        for gy in (top, (top + zero_y) / 2.0) if pos_span > 36 else ():
            parts.append(
                f'<line x1="{_PAD}" y1="{gy:.1f}" x2="{width - _PAD}" y2="{gy:.1f}" '
                f'stroke="{pal.grid}" stroke-dasharray="4 4"/>'
            )

        slot = plot_w / n_cat
        gap_in = 4.0 if n_ser > 1 else 0.0
        bar_w = min((slot * 0.64 - gap_in * (n_ser - 1)) / n_ser, 52.0)
        bar_w = max(8.0, bar_w)
        group_w = bar_w * n_ser + gap_in * (n_ser - 1)

        for ci, lab in enumerate(labels):
            gx = _PAD + ci * slot + (slot - group_w) / 2
            for si, ser in enumerate(named):
                if lab not in maps[si]:
                    continue
                pt = maps[si][lab]
                val = pt.value
                if val >= 0:
                    denom = pos_max if pos_max > 0 else 1.0
                    raw_h = (val / denom) * pos_span if pos_span > 0 else 0.0
                else:
                    denom = neg_max if neg_max > 0 else 1.0
                    raw_h = (abs(val) / denom) * neg_span if neg_span > 0 else 0.0
                h = max(4.0, raw_h) if val != 0 else 1.5
                x = gx + si * (bar_w + gap_in)
                y = zero_y - h if val >= 0 else zero_y
                fill = pt.color or _bar_fill(value=val, series_color=ser.color, signed=signed, dark=dark)
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{h:.1f}" rx="5" fill="{fill}"/>'
                )
                if n_cat * n_ser <= 16:
                    ty = y - 7.0 if val >= 0 else y + h + 15.0
                    lbl_fill = fill if signed else pal.val
                    parts.append(
                        f'<text x="{x + bar_w / 2:.1f}" y="{ty:.1f}" font-size="12.5" font-weight="700" '
                        f'fill="{lbl_fill}" text-anchor="middle">'
                        f"{_esc(_fmt(val, unit=pt.unit, signed=signed))}</text>"
                    )
            parts.append(
                f'<text x="{_PAD + ci * slot + slot / 2:.1f}" y="{height - 10:.1f}" font-size="12.5" '
                f'font-weight="500" fill="{pal.text}" text-anchor="middle">{_clip_label(lab, 12)}</text>'
            )
        parts.append(
            f'<line x1="{_PAD}" y1="{zero_y:.1f}" x2="{width - _PAD}" y2="{zero_y:.1f}" '
            f'stroke="{pal.axis}" stroke-width="1.3"/>'
        )
    else:
        max_lab = max((len(lab) for lab in labels), default=4)
        neg_texts = [_fmt(p.value, unit=p.unit, signed=signed) for p in pts_all if p.value < 0]
        pos_texts = [_fmt(p.value, unit=p.unit, signed=signed) for p in pts_all if p.value >= 0]
        lab_ratio = 0.29 if neg_texts else 0.34
        label_w = float(min(width * lab_ratio, 180.0, max(80.0, max_lab * 12.5 + 16.0)))
        neg_gutter = (
            min(float(width) * 0.26, max(_est_text_px(t_neg, 12.5) for t_neg in neg_texts) + 18.0) if neg_texts else 0.0
        )
        right_pad = (
            min(float(width) * 0.26, max(58.0, max(_est_text_px(t_pos, 12.5) for t_pos in pos_texts) + 20.0))
            if pos_texts
            else 24.0
        )
        plot_left = label_w + neg_gutter
        usable_w = max(72.0, width - plot_left - right_pad)
        top = float(head + 12)
        plot_h = max(80.0, float(height - top - 24))
        if bipolar:
            neg_ratio = max(0.22, min(0.78, neg_max / (pos_max + neg_max)))
            neg_span = usable_w * neg_ratio
            pos_span = usable_w - neg_span
            zero_x = plot_left + neg_span
        elif only_neg:
            neg_span = usable_w
            pos_span = 0.0
            zero_x = plot_left + usable_w
        else:
            neg_span = 0.0
            pos_span = usable_w
            zero_x = plot_left

        slot = plot_h / n_cat
        bar_h = min(slot * 0.64 / n_ser, 22.0)
        bar_h = max(8.0, bar_h)
        lab_cap = max(5, min(14, int((label_w - 14.0) / 12.2)))
        for ci, lab in enumerate(labels):
            gy = top + ci * slot + (slot - bar_h * n_ser) / 2
            parts.append(
                f'<text x="{label_w - 10:.1f}" y="{gy + (bar_h * n_ser) / 2 + 4.5:.1f}" font-size="13" '
                f'font-weight="500" fill="{pal.text}" text-anchor="end">{_clip_label(lab, lab_cap)}</text>'
            )
            for si, ser in enumerate(named):
                if lab not in maps[si]:
                    continue
                pt = maps[si][lab]
                val = pt.value
                if val >= 0:
                    denom = pos_max if pos_max > 0 else 1.0
                    raw_w = (val / denom) * pos_span if pos_span > 0 else 0.0
                else:
                    denom = neg_max if neg_max > 0 else 1.0
                    raw_w = (abs(val) / denom) * neg_span if neg_span > 0 else 0.0
                w = max(4.0, raw_w) if val != 0 else 1.5
                y = gy + si * bar_h
                x = zero_x if val >= 0 else zero_x - w
                fill = pt.color or _bar_fill(value=val, series_color=ser.color, signed=signed, dark=dark)
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{bar_h:.1f}" rx="5" fill="{fill}"/>'
                )
                tx = x + w + 7.0 if val >= 0 else x - 7.0
                anchor = "start" if val >= 0 else "end"
                lbl_fill = fill if signed else pal.val
                parts.append(
                    f'<text x="{tx:.1f}" y="{y + bar_h / 2 + 4.5:.1f}" font-size="12.5" font-weight="700" '
                    f'fill="{lbl_fill}" text-anchor="{anchor}">'
                    f"{_esc(_fmt(val, unit=pt.unit, signed=signed))}</text>"
                )
        if bipolar or only_neg:
            parts.append(
                f'<line x1="{zero_x:.1f}" y1="{top:.1f}" x2="{zero_x:.1f}" y2="{top + plot_h:.1f}" '
                f'stroke="{pal.axis}" stroke-width="1.3"/>'
            )
    parts.append("</svg>")
    return "".join(parts)


def _svg_benchmark(
    points: Sequence[_Point],
    *,
    width: int,
    dark: bool,
    title: str,
    accent_override: str = "",
) -> str:
    """横向胶囊对比条（适合性能/耗时/份额/指标横向对标，支持胜出项高亮与副标题）。"""
    if not points:
        return "⚠️ benchmark 缺少 data 数据点"
    pal = _palette(dark)
    has_sub = any(bool(p.sub) for p in points)
    row_h = 54 if has_sub else 44
    head = _TITLE_H + 8 if title else 14
    height = max(150, head + len(points) * row_h + 18)
    max_lab = max((len(p.label) for p in points), default=6)
    max_sub = max((len(p.sub) for p in points), default=0)
    col_chars = max(max_lab, int(max_sub * 0.72))
    label_w = float(min(width * 0.35, 205.0, max(92.0, col_chars * 12.5 + 18.0)))
    max_label_chars = max(6, int((label_w - 12.0) / 13.0))
    right_val_pad = float(max(82.0, min(112.0, width * 0.23)))
    track_x = float(_PAD) + label_w
    track_w = max(90.0, float(width - _PAD) - track_x)
    fill_max_w = max(64.0, track_w - right_val_pad)
    vmax = max((abs(p.value) for p in points), default=1.0) or 1.0
    any_hl = any(p.highlight for p in points)
    peers = _BENCH_PEERS_DARK if dark else _BENCH_PEERS_LIGHT
    accent = _color_or_default(accent_override, pal.accent) if accent_override else pal.accent

    parts = [_svg_open(width, height)]
    if title:
        parts.append(
            f'<text x="{_PAD:.1f}" y="26" font-size="16" font-weight="700" '
            f'fill="{pal.title}" text-anchor="start">{_clip_label(title, 42)}</text>'
        )

    bar_h = 24.0
    peer_idx = 0
    for i, pt in enumerate(points):
        cy = float(head + i * row_h + row_h / 2.0)
        is_hl = pt.highlight
        if pt.color:
            fill = pt.color
        elif any_hl:
            if is_hl:
                fill = accent
            else:
                fill = peers[peer_idx % len(peers)]
                peer_idx += 1
        else:
            fill = accent if (i == 0 and accent_override) else _SERIES_COLORS[i % len(_SERIES_COLORS)]

        lab_weight = "700" if is_hl else "600"
        lab_color = pal.title if is_hl else pal.text
        if pt.sub:
            parts.append(
                f'<text x="{_PAD:.1f}" y="{cy - 2:.1f}" font-size="14" font-weight="{lab_weight}" '
                f'fill="{lab_color}" text-anchor="start">{_clip_label(pt.label, max_label_chars)}</text>'
            )
            parts.append(
                f'<text x="{_PAD:.1f}" y="{cy + 15:.1f}" font-size="11.5" font-weight="400" '
                f'fill="{pal.muted}" text-anchor="start">{_clip_label(pt.sub, max_label_chars + 6)}</text>'
            )
        else:
            parts.append(
                f'<text x="{_PAD:.1f}" y="{cy + 5:.1f}" font-size="14" font-weight="{lab_weight}" '
                f'fill="{lab_color}" text-anchor="start">{_clip_label(pt.label, max_label_chars)}</text>'
            )

        ty = cy - bar_h / 2.0
        parts.append(
            f'<rect x="{track_x:.1f}" y="{ty:.1f}" width="{track_w:.1f}" height="{bar_h:.1f}" '
            f'rx="8" fill="{pal.track}"/>'
        )
        bar_w = max(12.0, abs(pt.value) / vmax * fill_max_w) if pt.value != 0 else 4.0
        if is_hl:
            parts.append(
                f'<rect x="{track_x - 1.5:.1f}" y="{ty - 1.5:.1f}" width="{bar_w + 3:.1f}" '
                f'height="{bar_h + 3:.1f}" rx="9" fill="{fill}" fill-opacity="0.22"/>'
            )
        parts.append(
            f'<rect x="{track_x:.1f}" y="{ty:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="8" fill="{fill}"/>'
        )
        val_weight = "700"
        val_size = "16" if is_hl else "14.5"
        val_color = pal.title if is_hl else pal.text
        val_x = track_x + bar_w + 10.0
        parts.append(
            f'<text x="{val_x:.1f}" y="{cy + 5.5:.1f}" font-size="{val_size}" font-weight="{val_weight}" '
            f'fill="{val_color}" text-anchor="start">{_esc(_fmt(pt.value, unit=pt.unit))}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def _level_color(pt: _Point, dark: bool, fallback: str) -> str:
    if pt.color:
        return pt.color
    if pt.highlight or pt.role == "current":
        return "#22d3ee" if dark else "#0284c7"
    if pt.role in ("high", "resistance", "upper", "max"):
        return "#fb7185" if dark else "#e11d48"
    if pt.role in ("low", "support", "lower", "min"):
        return "#34d399" if dark else "#059669"
    if pt.role in ("target", "median", "mid"):
        return "#f59e0b" if dark else "#d97706"
    return fallback


def _svg_levels(
    points: Sequence[_Point],
    *,
    width: int,
    dark: bool,
    title: str,
) -> str:
    """非零锚定关键位阶梯图：放大局部区间 [min, max]，按高→低分行排列，彻底避免窄区间柱长雷同与标签重叠。"""
    if not points:
        return "⚠️ levels 缺少 data 数据点"
    pal = _palette(dark)
    ordered = sorted(points, key=lambda p: p.value, reverse=True)
    vals = [p.value for p in ordered]
    v_min, v_max = min(vals), max(vals)
    span = v_max - v_min
    if span <= 0:
        span = abs(v_max) * 0.1 or 1.0
    lo = v_min - span * 0.14
    hi = v_max + span * 0.18

    row_h = 40
    head = _TITLE_H + 14 if title else 20
    height = max(180, head + len(ordered) * row_h + 34)
    max_lab = max((len(p.label) for p in ordered), default=6)
    label_w = float(min(width * 0.34, 180.0, max(92.0, max_lab * 12.5 + 18.0)))
    pad_r = float(max(92.0, min(114.0, width * 0.22)))
    plot_x0 = float(_PAD) + label_w
    plot_w = max(100.0, width - plot_x0 - pad_r)

    def x_of(v: float) -> float:
        return plot_x0 + (v - lo) / (hi - lo) * plot_w

    cur_pts = [p for p in ordered if p.highlight or p.role == "current"]
    ref_pt = cur_pts[0] if cur_pts else None
    ref_x = x_of(ref_pt.value) if ref_pt is not None else None

    parts = [_svg_open(width, height)]
    if title:
        parts.append(
            f'<text x="{_PAD:.1f}" y="26" font-size="16" font-weight="700" '
            f'fill="{pal.title}" text-anchor="start">{_clip_label(title, 42)}</text>'
        )

    top_y = float(head)
    bot_y = float(head + len(ordered) * row_h)
    for gv in (v_min, (v_min + v_max) / 2.0, v_max):
        gx = x_of(gv)
        parts.append(
            f'<line x1="{gx:.1f}" y1="{top_y:.1f}" x2="{gx:.1f}" y2="{bot_y:.1f}" '
            f'stroke="{pal.grid}" stroke-dasharray="3 4"/>'
        )
        parts.append(
            f'<text x="{gx:.1f}" y="{height - 10:.1f}" font-size="11.5" fill="{pal.muted}" '
            f'text-anchor="middle">{_esc(_fmt(gv, unit=ordered[0].unit))}</text>'
        )

    if ref_x is not None and ref_pt is not None:
        ref_c = _level_color(ref_pt, dark, pal.accent)
        parts.append(
            f'<line x1="{ref_x:.1f}" y1="{top_y - 4:.1f}" x2="{ref_x:.1f}" y2="{bot_y + 4:.1f}" '
            f'stroke="{ref_c}" stroke-width="1.8" stroke-dasharray="5 3"/>'
        )
    else:
        spine = " ".join(
            f"{'M' if i == 0 else 'L'}{x_of(pt.value):.1f},{head + i * row_h + row_h / 2.0:.1f}"
            for i, pt in enumerate(ordered)
        )
        if len(ordered) >= 2:
            parts.append(
                f'<path d="{spine}" fill="none" stroke="{pal.axis}" stroke-width="2" stroke-linejoin="round"/>'
            )

    default_dot = "#a78bfa" if dark else "#6366f1"
    lv_lab_cap = max(5, min(14, int((plot_x0 - 16.0) / 12.2)))
    for i, pt in enumerate(ordered):
        cy = float(head + i * row_h + row_h / 2.0)
        px = x_of(pt.value)
        is_cur = pt.highlight or pt.role == "current"
        dot_c = _level_color(pt, dark, default_dot)

        parts.append(
            f'<rect x="{plot_x0:.1f}" y="{cy - 4:.1f}" width="{plot_w:.1f}" height="8" rx="4" fill="{pal.track}"/>'
        )
        if ref_x is not None and not is_cur:
            seg_x = min(ref_x, px)
            seg_w = max(3.0, abs(px - ref_x))
            parts.append(
                f'<rect x="{seg_x:.1f}" y="{cy - 4:.1f}" width="{seg_w:.1f}" height="8" rx="4" '
                f'fill="{dot_c}" fill-opacity="0.65"/>'
            )

        lab_w = "700" if is_cur else "500"
        lab_c = dot_c if is_cur else pal.text
        parts.append(
            f'<text x="{plot_x0 - 12:.1f}" y="{cy + 4.5:.1f}" font-size="13" font-weight="{lab_w}" '
            f'fill="{lab_c}" text-anchor="end">{_clip_label(pt.label, lv_lab_cap)}</text>'
        )

        if is_cur:
            parts.append(f'<circle cx="{px:.1f}" cy="{cy:.1f}" r="10" fill="{dot_c}" fill-opacity="0.24"/>')
            parts.append(
                f'<circle cx="{px:.1f}" cy="{cy:.1f}" r="6.5" fill="{dot_c}" '
                f'stroke="{pal.bg_stroke}" stroke-width="2"/>'
            )
        else:
            parts.append(
                f'<circle cx="{px:.1f}" cy="{cy:.1f}" r="5.5" fill="{dot_c}" '
                f'stroke="{pal.bg_stroke}" stroke-width="1.8"/>'
            )

        place_left = ref_x is not None and px < ref_x - 18 and px - plot_x0 > 68
        tx = px - 12.0 if place_left else px + 12.0
        anchor = "end" if place_left else "start"
        val_c = dot_c if is_cur else pal.val
        parts.append(
            f'<text x="{tx:.1f}" y="{cy + 4.5:.1f}" font-size="13" font-weight="700" '
            f'fill="{val_c}" text-anchor="{anchor}">{_esc(_fmt(pt.value, unit=pt.unit))}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def _sector_path(cx: float, cy: float, r_out: float, r_in: float, a1: float, a2: float) -> str:
    span = a2 - a1
    if span >= 2 * math.pi - 1e-4:
        a2 = a1 + 2 * math.pi - 1e-3
        span = a2 - a1
    large = 1 if span > math.pi else 0
    ox1, oy1 = cx + r_out * math.cos(a1), cy + r_out * math.sin(a1)
    ox2, oy2 = cx + r_out * math.cos(a2), cy + r_out * math.sin(a2)
    if r_in <= 0:
        return f"M{cx:.1f},{cy:.1f} L{ox1:.1f},{oy1:.1f} A{r_out:.1f},{r_out:.1f} 0 {large} 1 {ox2:.1f},{oy2:.1f} Z"
    ix2, iy2 = cx + r_in * math.cos(a2), cy + r_in * math.sin(a2)
    ix1, iy1 = cx + r_in * math.cos(a1), cy + r_in * math.sin(a1)
    return (
        f"M{ox1:.1f},{oy1:.1f} "
        f"A{r_out:.1f},{r_out:.1f} 0 {large} 1 {ox2:.1f},{oy2:.1f} "
        f"L{ix2:.1f},{iy2:.1f} "
        f"A{r_in:.1f},{r_in:.1f} 0 {large} 0 {ix1:.1f},{iy1:.1f} Z"
    )


def _svg_pie(
    series: Sequence[_Point],
    *,
    width: int,
    height: int,
    colors: Sequence[str],
    dark: bool,
    title: str,
    donut: bool,
) -> str:
    total = sum(max(0.0, p.value) for p in series)
    pal = _palette(dark)
    parts = [_svg_open(width, height)]
    _draw_title_legend(parts, width=width, title=title, named=[], legend=False, signed=False, dark=dark, pal=pal)
    if total <= 0:
        parts.append(
            f'<text x="{width / 2:.1f}" y="{height / 2:.1f}" font-size="14" '
            f'fill="{pal.text}" text-anchor="middle">无有效占比数据</text></svg>'
        )
        return "".join(parts)
    head = _header_offset(title=title, legend=False)
    cx = width * 0.28
    cy = head + (height - head) / 2.0
    r_out = min(cx - _PAD, (height - head) / 2.0 - 20.0)
    r_out = max(44.0, r_out)
    r_in = r_out * 0.58 if donut else 0.0
    angle = -math.pi / 2.0

    lx = width * 0.52
    leg_cap = max(5, min(14, int((width - _PAD - 56.0 - (lx + 18.0)) / 13.0)))
    legend_span = len(series) * 26.0
    ly = max(float(head + 24), cy - legend_span / 2.0 + 10.0)

    for i, p in enumerate(series):
        frac = max(0.0, p.value) / total
        fill = p.color if p.color else colors[i % len(colors)]
        if frac > 0:
            a2 = angle + frac * 2.0 * math.pi
            d = _sector_path(cx, cy, r_out, r_in, angle, a2)
            parts.append(f'<path d="{d}" fill="{fill}" stroke="{pal.bg_stroke}" stroke-width="2"/>')
            angle = a2
        parts.append(f'<rect x="{lx:.1f}" y="{ly - 11:.1f}" width="12" height="12" rx="3" fill="{fill}"/>')
        parts.append(
            f'<text x="{lx + 18:.1f}" y="{ly:.1f}" font-size="13" font-weight="500" fill="{pal.text}">'
            f"{_clip_label(p.label, leg_cap)}</text>"
        )
        parts.append(
            f'<text x="{width - _PAD:.1f}" y="{ly:.1f}" font-size="13" font-weight="700" '
            f'fill="{pal.val}" text-anchor="end">{frac * 100:.1f}%</text>'
        )
        ly += 26.0

    if donut:
        sample_unit = series[0].unit
        parts.append(
            f'<text x="{cx:.1f}" y="{cy - 6:.1f}" font-size="12" font-weight="500" '
            f'fill="{pal.muted}" text-anchor="middle">合计</text>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{cy + 16:.1f}" font-size="18" font-weight="700" '
            f'fill="{pal.title}" text-anchor="middle">{_esc(_fmt(total, unit=sample_unit))}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def chart_spec_to_svg(spec: Mapping[str, ChartSpecValue]) -> str:
    """chart_spec → 内联 SVG。错误时返回 ``⚠️`` 文案（工具层直接透传）。"""
    chart_type = str(spec["type"]).strip().lower() if "type" in spec and spec["type"] is not None else ""
    unit = str(spec["unit"]).strip() if "unit" in spec and spec["unit"] is not None else ""
    named = _parse_named_series(spec, unit)
    if not named:
        return "⚠️ chart_spec 缺少 data / series 数据点（需要 [{label, value}, ...] 或 series[]）"
    width = int(_num(spec["width"] if "width" in spec else _DEFAULT_W)) or _DEFAULT_W
    height = int(_num(spec["height"] if "height" in spec else _DEFAULT_H)) or _DEFAULT_H
    width = max(240, min(width, 1200))
    height = max(160, min(height, 960))
    dark = bool(spec["dark"]) if "dark" in spec else True
    signed = bool(spec["signed"]) if "signed" in spec else False
    title = str(spec["title"]).strip() if "title" in spec and spec["title"] else ""
    legend = bool(spec["legend"]) if "legend" in spec else True
    raw_color = str(spec["color"]).strip() if "color" in spec and isinstance(spec["color"], str) else ""

    if chart_type == "line":
        return _svg_line(named, width=width, height=height, dark=dark, title=title, legend=legend, unit=unit)
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
    if chart_type == "benchmark":
        return _svg_benchmark(named[0].points, width=width, dark=dark, title=title, accent_override=raw_color)
    if chart_type in ("levels", "range"):
        return _svg_levels(named[0].points, width=width, dark=dark, title=title)
    if chart_type in ("pie", "donut"):
        return _svg_pie(
            named[0].points,
            width=width,
            height=height,
            colors=_SERIES_COLORS,
            dark=dark,
            title=title,
            donut=chart_type == "donut",
        )
    return "⚠️ chart_spec.type 只支持 line / bar / hbar / benchmark / levels / donut / pie"


@ai_tools(category="media", capability_domain="资料出图", code_callable=False)
async def render_chart_spec(
    ctx: RunContext[ToolContext],
    type: str,
    data: list[ChartPointInput] | None = None,
    series: list[ChartSeriesInput] | None = None,
    width: int = _DEFAULT_W,
    height: int = _DEFAULT_H,
    color: str = "",
    dark: bool = True,
    signed: bool = False,
    title: str = "",
    legend: bool = True,
    unit: str = "",
) -> str:
    """把声明式图表规格渲染成高清晰度内联 SVG 片段（渲染引擎无 JS）。

    图表选型准则（按数据形态选最直观的一种，禁止混用悬殊量纲）：
    - ``line``：时间序列/趋势曲线（自动缩放局部波动区间并标出首尾与极值）。
    - ``bar`` / ``hbar``：类目对比或正负增减（有正负含义必开 ``signed=True``，正负轴按极值比例自适应；
      勿把差 50 倍以上的极端离群值塞进同一线性柱图，极端值宜提做顶部大字指标）。
    - ``benchmark``：横向胶囊轨道对标图（适合 3–8 项耗时/得分/估值/份额横向 PK，
      支持 ``sub`` 副标题与 ``highlight=True`` 胜出项高亮发光）。
    - ``levels``：非零锚定关键位阶梯图（适合窄区间内的当前值与上下界/均值线/分位点对比，
      自动放大 [min, max] 区间并画出与 ``role="current"`` 当前位的偏离，绝不从 0 画一排等长柱）。
    - ``donut`` / ``pie``：结构占比环形图（中心显示合计）或饼图。

    多实体 × 多指标对比传 ``series``（每实体一个 ``name``），不要把身份拍扁进单根柱的 label。

    Args:
        ctx: 工具执行上下文。
        type: line / bar / hbar / benchmark / levels / donut / pie。
        data: 单系列数据点 [{label, value, sub?, unit?, highlight?, role?, color?}, ...]。
            role 可选 "current"(当前值基准线) / "high"(上界) / "low"(下界) / "target"(目标位)。
        series: 多系列 [{name, data: [{label, value}], color?}, ...]，仅用于 line / bar / hbar。
        width: SVG 宽度 px，默认 720（与标准单栏卡片等宽，字号 1:1 最清晰；双栏卡片可传 360）。
        height: SVG 高度 px，默认 380（benchmark / levels 会按行数自动适配高度）。
        color: 单系列或高亮主色（#hex）。
        dark: 暗色底传 True，浅色底传 False（务必与卡片底色一致以保证字色对比度）。
        signed: True 时正负值自动取涨/跌语义色并带正号。
        title: 可选图题（若外层卡片已有标题可留空免重复）。
        legend: 多系列时画图例，默认 True。
        unit: 数值后缀单位（如 "%"、"秒"、"元"、"x"），也可直接写在 value 字符串末尾。

    Returns:
        带 ``{{chart_xxxxxxxx}}`` 快捷占位句柄头注释的内联 ``<svg>…</svg>`` 片段；失败时返回 ⚠️ 说明。
    """
    _ = ctx
    spec: dict[str, ChartSpecValue] = {
        "type": type,
        "width": width,
        "height": height,
        "dark": dark,
        "signed": signed,
        "legend": legend,
    }
    if unit:
        spec["unit"] = unit
    if title:
        spec["title"] = title
    if series:
        spec["series"] = series
    elif data:
        spec["data"] = data
    if color:
        spec["color"] = color
    svg = chart_spec_to_svg(spec)
    if not svg.startswith("<svg"):
        return svg
    cid = register_chart_svg(svg)
    return f"<!-- chart_id={cid}（HTML 中可直接写 {{{{{cid}}}}} 自动展开完整 SVG，免手抄长标签） -->\n{svg}"
