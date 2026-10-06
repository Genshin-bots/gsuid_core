"""更新记录卡片模板：把结构化记录拼成整页 HTML。

版式照参考图来：左上超大版本号 + 标题两行，右侧轨道球装饰，底下一条信息带，
再一块细线引言面板（右侧带迷你指标卡），条目走**两栏**排，底部居中标语。

前几版栽在同一个地方：把内容塞进一堆圆角面板。这版只在引言处留一块细线面板，
其余全靠留白、字重和一根竖线分栏。底色是近乎纯黑 + 右上暖光，不是星空壁纸。
"""

from __future__ import annotations

import re
import html
import random
from typing import Sequence

from .changelog import VersionRef, ChangelogEntry, ChangelogVersion

# 逻辑宽（CSS px）。设备像素 = 本值 * DEVICE_SCALE，渲染时 max_width / dpi 同步翻倍。
LAYOUT_WIDTH = 480
DEVICE_SCALE = 2

_MAX_COMMITS = 8
_INDEX_LIMIT = 12

_STAR_W = 480
_STAR_H = 9600
_STAR_SEED = 7
_STAR_COUNT = 190

# 金调是这套版式的主色，版本号用奶油白，标题与指标用金
_CREAM = "#efe3cd"
_GOLD = "#e0ae4c"
_GOLD_SOFT = "#c99a45"
_ORANGE = "#f08a3c"
_MUTED = "#8a8a96"

_ACCENTS: dict[str, str] = {
    "✨": "#4ade80",
    "🐛": "#f87171",
    "🎨": "#c084fc",
    "⚡": "#60a5fa",
    "💥": "#ef4444",
    "🔒": "#38bdf8",
    "⬆️": "#94a3b8",
    "♻": "#a78bfa",
    "🔧": "#5eead4",
    "📦": "#94a3b8",
    "🍱": "#fbbf24",
    "📝": "#94a3b8",
    "🚨": "#fb7185",
    "🧪": "#34d399",
    "👽": "#94a3b8",
    "🌐": "#22d3ee",
    "💻": "#38bdf8",
    "💚": "#4ade80",
    "🔖": "#fbbf24",
    "✏️": "#94a3b8",
    "🚀": "#60a5fa",
    "📌": "#fbbf24",
    "⚗️": "#818cf8",
    "🔌": "#2dd4bf",
    "🔥": "#f87171",
}
_DEFAULT_ACCENT = "#94a3b8"

# 仓库 commit emoji 约定里没有 icon 的类别，给个中性点
_FALLBACK_ICON = "◆"
_SLOGAN = "让智能更有温度"

# 「重要」的定义：带单位的量化事实。版本号 0.11.0 不会被误命中（后面跟的是点不是单位）。
_HL_UNITS = "倍|条|次|个|万|亿|%|ms|s|x|X|秒|分钟|小时|天|年|人|项|处|版|款|台|档"
_HL_RE = re.compile(rf"\d+(?:\.\d+)?\s*(?:{_HL_UNITS})(?![a-zA-Z])")
_METRIC_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:倍|%|ms|x|X)")
_CODE_SPLIT_RE = re.compile(r"(`[^`]+`)")


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _rgba(color: str, alpha: float) -> str:
    value = color.lstrip("#")
    return f"rgba({int(value[0:2], 16)},{int(value[2:4], 16)},{int(value[4:6], 16)},{alpha})"


def _icon_box(inner: str, size: int) -> str:
    """用外层容器定尺寸。

    实测（eval/manual/probe_svg_size4.py）：pytakumi 下 inline <svg> 的
    width/height 属性与 CSS width 一律失效，必须由父容器约束，否则会被拉到整行宽。
    """
    return f'<span class="ic" style="width:{size}px;height:{size}px">{inner}</span>'


def _svg_body(shape: str, color: str) -> str:
    """图标：属性挂在 <g> 上。若把 stroke 直接跟在 shape 后面就成了文本节点，fill 退回黑。"""
    attrs = f'stroke="{color}" stroke-width="1.5" fill="none" stroke-linecap="round"'
    inner = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
        f"<g {attrs}>{shape}</g></svg>"
    )
    return _icon_box(inner, 13)


def _icon_calendar(color: str) -> str:
    shape = (
        '<rect x="2" y="3.5" width="12" height="10.5" rx="2"/>'
        '<line x1="2" y1="7" x2="14" y2="7"/>'
        '<line x1="5.5" y1="1.8" x2="5.5" y2="4.6"/>'
        '<line x1="10.5" y1="1.8" x2="10.5" y2="4.6"/>'
    )
    return _svg_body(shape, color)


def _icon_version(color: str) -> str:
    shape = '<polyline points="4,2 4,6 12,6"/><polyline points="12,14 12,10 4,10"/>'
    return _svg_body(shape, color)


def _icon_code(color: str) -> str:
    shape = '<polyline points="5.5,4 2,8 5.5,12"/><polyline points="10.5,4 14,8 10.5,12"/>'
    return _svg_body(shape, color)


def _sparkline(width: int, height: int) -> str:
    """上升折线：给迷你指标卡当视觉配重。"""
    points = [
        (2, height - 3),
        (width * 0.28, height - 9),
        (width * 0.52, height - 13),
        (width * 0.74, height - 22),
        (width - 2, 3),
    ]
    path = " ".join(f"{int(x)},{int(y)}" for x, y in points)
    area = f"0,{height} {path} {width},{height}"
    inner = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<polygon points="{area}" fill="{_GOLD}" opacity="0.13"/>'
        f'<polyline points="{path}" fill="none" stroke="{_GOLD}" stroke-width="1.6" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{width - 2}" cy="3" r="2.4" fill="{_GOLD}"/></svg>'
    )
    return f'<span class="ic" style="width:{width}px;height:{height}px">{inner}</span>'


def _sparkle(cx: int, cy: int, size: int, color: str) -> str:
    return (
        f'<path d="M{cx} {cy - size} L{cx + size * 0.22} {cy - size * 0.22} L{cx + size} {cy} '
        f"L{cx + size * 0.22} {cy + size * 0.22} L{cx} {cy + size} "
        f"L{cx - size * 0.22} {cy + size * 0.22} L{cx - size} {cy} "
        f'L{cx - size * 0.22} {cy - size * 0.22} Z" fill="{color}"/>'
    )


def _orb_svg(size: int = 150) -> str:
    """轨道球：同心圆叠出球体，旋转椭圆叠出金色轨道。"""
    c = size / 2
    ball = []
    for index in range(16):
        ratio = 1.0 - index / 16
        ball.append(f'<circle cx="{c:.1f}" cy="{c:.1f}" r="{c * ratio:.1f}" fill="url(#orbGrad)" opacity="0.16"/>')
    rings = []
    for tilt, rx, opacity in ((-22, 0.94, 0.85), (-22, 0.72, 0.55), (-22, 0.50, 0.35)):
        ry = rx * 0.30
        rings.append(
            f'<ellipse cx="{c:.1f}" cy="{c:.1f}" rx="{c * rx:.1f}" ry="{c * ry:.1f}" '
            f'fill="none" stroke="{_GOLD}" stroke-width="1.2" opacity="{opacity}" '
            f'transform="rotate({tilt} {c:.1f} {c:.1f})"/>'
        )
    rim = (
        f'<circle cx="{c:.1f}" cy="{c:.1f}" r="{c - 1:.1f}" fill="none" '
        'stroke="#ffffff" stroke-width="1" opacity="0.22"/>'
    )
    inner = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">'
        '<defs><radialGradient id="orbGrad" cx="34%" cy="30%">'
        '<stop offset="0%" stop-color="#ffffff"/>'
        '<stop offset="55%" stop-color="#9fb6d8"/>'
        '<stop offset="100%" stop-color="#2b3550"/>'
        "</radialGradient></defs>"
        f"{''.join(ball)}{rim}{''.join(rings)}"
        f"{_sparkle(int(size * 0.14), int(size * 0.26), 7, _CREAM)}"
        f"{_sparkle(int(size * 0.86), int(size * 0.60), 5, _GOLD)}"
        "</svg>"
    )
    return _icon_box(inner, size)


def _starfield_data_uri() -> str:
    """近黑底 + 右上暖光 + 稀星。参考图不是星空壁纸，别把字埋了。"""
    rng = random.Random(_STAR_SEED)
    width = _STAR_W
    height = _STAR_H
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        f'<rect width="{width}" height="{height}" fill="#07070c"/>',
    ]
    # 右上暖光：同心椭圆叠柔光，SVG 没有模糊滤镜
    for layer in range(16):
        ratio = 1.0 - layer / 16
        parts.append(
            f'<ellipse cx="{int(width * 0.92)}" cy="{int(height * 0.03)}" '
            f'rx="{int(260 * ratio)}" ry="{int(220 * ratio)}" fill="#7a5a22" opacity="0.035"/>'
        )
    for layer in range(12):
        ratio = 1.0 - layer / 12
        parts.append(
            f'<ellipse cx="{int(width * 0.04)}" cy="{int(height * 0.42)}" '
            f'rx="{int(200 * ratio)}" ry="{int(240 * ratio)}" fill="#1d3a5c" opacity="0.030"/>'
        )
    for _ in range(_STAR_COUNT):
        x = rng.randint(2, width - 3)
        y = rng.randint(2, height - 3)
        radius = round(rng.uniform(0.3, 0.9), 2)
        opacity = round(rng.uniform(0.10, 0.5), 2)
        tint = rng.choice(("#ffffff", "#cfe0ff", "#ffeccd"))
        parts.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="{tint}" opacity="{opacity}"/>')
    parts.append("</svg>")
    import base64

    payload = base64.b64encode("".join(parts).encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{payload}"


_STARFIELD_URI = _starfield_data_uri()

_CSS = (
    """*{margin:0;padding:0;}
body{
  width:480px;overflow:hidden;
  background-image:url("__STARFIELD__");
  background-repeat:repeat-y;background-size:480px 9600px;background-color:#07070c;
  font-family:"MiSans","PingFang SC","Microsoft YaHei",sans-serif;
  color:#f0f0f2;line-height:1.5;padding:24px 22px 20px 22px;
}
/* orb 绝对定位溢出到标题区外侧，标题才能占满整行不折行；min-height 给它留位 */
.head{position:relative;min-height:152px;}
.head-main{flex:1;}
/* orb 绝对定位到右上并溢出到标题区外侧，标题才能占满整行不折行 */
.head-side{position:absolute;top:-6px;right:-2px;width:186px;height:150px;pointer-events:none;}
.orb{position:absolute;top:0;right:36px;}
.slogan{position:absolute;top:78px;right:0;font-size:12px;font-weight:600;color:#8d8d99;
  line-height:1.75;text-align:right;white-space:nowrap;letter-spacing:0.06em;}
.eyebrow{font-size:9.5px;font-weight:700;letter-spacing:0.42em;color:{GOLD};text-transform:uppercase;}
.ver{font-size:52px;font-weight:600;line-height:1.02;letter-spacing:1px;color:{CREAM};margin-top:8px;}
/* 标题：橙金 + 斜体。倾斜只能靠 font-style——实测本渲染器不认 transform。 */
.title{margin-top:8px;font-size:16px;font-weight:700;line-height:1.45;
  color:{ORANGE};font-style:italic;letter-spacing:0.01em;}
.title+.title{margin-top:5px;font-size:14.5px;font-weight:600;}
.gold{color:{GOLD};}
.metabar{display:flex;align-items:center;margin-top:15px;padding:9px 0 0 0;
  border-top:1px solid rgba(255,255,255,0.09);}
.ic{display:inline-block;overflow:hidden;flex:none;line-height:0;vertical-align:middle;}
.mi{display:flex;align-items:center;font-size:10.5px;color:#9a9aa6;margin-right:11px;
  white-space:nowrap;}
.mi .lb{margin-left:5px;}
.vline{width:1px;height:11px;background:rgba(255,255,255,0.14);margin-right:11px;}
.backlink{margin-left:auto;font-size:10.5px;font-weight:600;color:#5b8def;white-space:nowrap;}
.lead{display:flex;margin-top:15px;padding:13px 14px 14px 12px;border-radius:10px;
  border:1px solid rgba(255,255,255,0.10);background:rgba(255,255,255,0.022);}
.quote{font-size:26px;font-weight:700;color:#5b8def;line-height:1;margin-right:9px;}
.lead-main{flex:1;}
.lead-hl{font-size:14px;font-weight:700;color:#f4f4f6;line-height:1.6;}
.lead-rest{margin-top:5px;font-size:11px;color:#8e8e9a;line-height:1.8;}
.stat{width:106px;flex:none;padding:10px 8px 8px 8px;border-radius:9px;
  border:1px solid rgba(255,255,255,0.10);background:rgba(255,255,255,0.022);
  display:flex;flex-direction:column;align-items:center;justify-content:center;}
.stat-val{font-size:24px;font-weight:700;color:{GOLD};line-height:1.1;}
.stat-lab{margin-top:2px;font-size:9px;color:#8d8d99;}
.spark{margin-top:5px;}
.spark svg{display:block;}
.cols{display:flex;margin-top:16px;}
.col{flex:1;padding-right:11px;}
.col+.col{border-left:1px solid rgba(255,255,255,0.08);padding-right:0;padding-left:13px;}
.entry{display:flex;margin-bottom:11px;}
.entry:last-child{margin-bottom:0;}
.ico{width:27px;flex:none;font-size:19px;line-height:1.35;}
.entry-body{flex:1;}
.pill{display:inline-block;padding:1px 8px;border-radius:9px;font-size:10px;font-weight:700;
  white-space:nowrap;word-break:keep-all;}
.txt{margin-top:4px;font-size:11px;color:#e8e8ec;line-height:1.72;}
.hl{color:{GOLD};font-weight:600;}
/* 标题底色已是橙色，金色数字叠上去会糊，标题内改用奶油白提亮 */
.title .hl{color:{CREAM};font-weight:700;}
.code{font-family:"Mono","Consolas",monospace;font-size:10.5px;padding:0 3px;border-radius:3px;
  background:rgba(255,255,255,0.07);color:#c8d4e8;}
.shas{margin-top:3px;font-family:"Mono","Consolas",monospace;font-size:8.5px;
  color:#6b6b78;line-height:1.5;word-break:break-all;}
.row{padding:6px 0;}
.row+.row{border-top:1px solid rgba(255,255,255,0.06);}
.row-ver{font-size:12.5px;font-weight:700;color:#f0f0f2;line-height:1.4;}
.row-sum{margin-top:2px;font-size:10px;color:#9a9aa6;line-height:1.6;}
.row-meta{margin-top:2px;font-size:8.5px;color:#6b6b78;line-height:1.6;}
.unreleased{margin-right:6px;font-weight:700;color:{GOLD};white-space:nowrap;}
.empty{margin-top:16px;}
.empty-title{font-size:14px;font-weight:600;color:#f0f0f2;}
.empty-text{margin-top:8px;font-size:11px;color:#8e8e9a;line-height:1.85;}
.foot{margin-top:18px;text-align:center;font-size:10px;color:#5d5d69;letter-spacing:0.12em;}""".replace(
        "{GOLD_SOFT}", _GOLD_SOFT
    )
    .replace("{CREAM}", _CREAM)
    .replace("{GOLD}", _GOLD)
    .replace("{ORANGE}", _ORANGE)
)


def _highlight(segment: str) -> str:
    return _HL_RE.sub(lambda matched: f'<span class="hl">{matched.group(0)}</span>', segment)


def _rich(text: str) -> str:
    """转义 → 反引号片段转等宽样式 → 其余文字高亮量化事实。

    顺序不能换：高亮只在非代码片段上跑，否则 `0.10.8` 这类标识符会被涂成金色。
    """
    pieces: list[str] = []
    for chunk in _CODE_SPLIT_RE.split(_esc(text)):
        if not chunk:
            continue
        if chunk.startswith("`") and chunk.endswith("`"):
            pieces.append(f'<span class="code">{chunk[1:-1]}</span>')
        else:
            pieces.append(_highlight(chunk))
    return "".join(pieces)


def _metric(text: str) -> str:
    matched = _METRIC_RE.search(text)
    return matched.group(0) if matched is not None else ""


def _document(body: str) -> str:
    css = _CSS.replace("__STARFIELD__", _STARFIELD_URI)
    return f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>{css}</style></head><body>{body}</body></html>'


def _brand(subtitle: str) -> str:
    return '<div class="eyebrow">Changelog</div>'


def _title_line(text: str) -> str:
    """标题行统一形态：`[ 文本 ]` + 斜体 + 橙金。索引卡与空卡也走这里，保证同一套语言。"""
    return f'<div class="title">[ {_highlight(_esc(text))} ]</div>'


def _title_block(version: ChangelogVersion) -> str:
    first, second = _split_subtitle(version.subtitle)
    first_html = _title_line(first)
    second_html = _title_line(second) if second else ""
    return (
        f'<div class="head"><div class="head-main">{_brand(version.subtitle)}'
        f'<div class="ver">{_esc(version.version)}</div>{first_html}{second_html}</div>'
        f'<div class="head-side"><div class="orb">{_orb_svg()}</div>'
        f'<div class="slogan">更快<br>更准<br>更智能</div></div></div>'
    )


def _meta_item(icon: str, label: str) -> str:
    return f'<span class="mi">{icon}<span class="lb">{label}</span></span>'


def _meta_bar(version: ChangelogVersion) -> str:
    items: list[str] = []
    if version.date:
        items.append(_meta_item(_icon_calendar(_MUTED), f"发布于 {version.date}"))
        items.append('<span class="vline"></span>')
    if version.commit_count > 0:
        items.append(_meta_item(_icon_version(_MUTED), f"本版 {version.commit_count} 个提交"))
        items.append('<span class="vline"></span>')
    items.append(_meta_item(_icon_code(_MUTED), "提交范围见 pyproject.toml"))
    return f'<div class="metabar">{"".join(items)}<span class="backlink">&lt; 返回索引</span></div>'


def _entry_html(entry: ChangelogEntry) -> str:
    accent = _ACCENTS.get(entry.emoji, _DEFAULT_ACCENT)
    icon = entry.emoji if entry.emoji in _ACCENTS else _FALLBACK_ICON
    commits = entry.commits[:_MAX_COMMITS]
    rest = len(entry.commits) - len(commits)
    sha_text = " · ".join(commits)
    if rest > 0:
        sha_text += f" · +{rest}"
    pill = f'<span class="pill" style="background:{_rgba(accent, 0.17)};color:{accent};">{_esc(entry.label)}</span>'
    txt = f'<div class="txt">{_rich(entry.text)}</div>' if entry.text else ""
    shas = f'<div class="shas">{sha_text}</div>' if sha_text else ""
    return f'<div class="entry"><div class="ico">{icon}</div><div class="entry-body">{pill}{txt}{shas}</div></div>'


def _lead_block(version: ChangelogVersion) -> str:
    if not version.lead:
        return ""
    first = version.lead[0]
    head, sep, tail = first.partition("。")
    lead_hl = f"{head}。" if sep else first
    rest_blocks = list(version.lead[1:])
    if tail.strip():
        rest_blocks.insert(0, tail.strip())
    lead_rest = f'<div class="lead-rest">{_rich(" ".join(rest_blocks))}</div>' if rest_blocks else ""

    metric = _metric(" ".join(rest_blocks)) or _metric(first)
    stat = ""
    if metric:
        stat = (
            '<div class="stat"><div class="stat-val">'
            f'{_esc(metric)}</div><div class="stat-lab">本版关键指标</div>'
            f'<div class="spark">{_sparkline(84, 26)}</div></div>'
        )
    return (
        f'<div class="lead"><div class="quote">❝</div><div class="lead-main">'
        f'<div class="lead-hl">{_rich(lead_hl)}</div>{lead_rest}</div>{stat}</div>'
    )


def _split_subtitle(subtitle: str) -> tuple[str, str]:
    """`消息吞吐最优 3.3x，记忆注入预算统一` → 首句 + 余下，与参考图的两行标题一致。"""
    head, sep, tail = subtitle.partition("，")
    if not sep or not tail.strip():
        return subtitle, ""
    return head, tail


def build_version_html(version: ChangelogVersion, *, is_current: bool) -> str:
    """单版本卡片。`is_current` 只影响信息带右侧的版本态标记。"""
    body = (
        f"{_title_block(version)}{_meta_bar(version)}{_lead_block(version)}"
        '<div class="cols">' + _split_columns(version.entries) + "</div>"
        f'<div class="foot">— {_esc(_SLOGAN)} —</div>'
    )
    return _document(body)


def _split_columns(entries: Sequence[ChangelogEntry]) -> str:
    """两栏均分：条目多时右栏接续，不足一栏则右栏留空。"""
    half = (len(entries) + 1) // 2
    left = "".join(_entry_html(entry) for entry in entries[:half])
    right = "".join(_entry_html(entry) for entry in entries[half:])
    return f'<div class="col">{left}</div><div class="col">{right}</div>'


def _index_date(ref: VersionRef) -> str:
    """索引表日期里带了「（未发布）」，同排已有未发布标记，去掉免得重复。"""
    if ref.released:
        return ref.date
    return ref.date.replace("（未发布）", "").strip()


def build_index_html(refs: Sequence[VersionRef], *, total: int) -> str:
    """版本索引卡片：只列最近若干版，其余给提示，避免一图拉太长。"""
    shown = list(refs[:_INDEX_LIMIT])
    rows: list[str] = []
    for ref in shown:
        mark = "" if ref.released else '<span class="unreleased">未发布</span>'
        # 未发布与已发布可能同版本号，标题补后缀免得两行看着像重复
        name = ref.version if ref.released else f"{ref.version}-unreleased"
        meta_bits: list[str] = []
        date = _index_date(ref)
        if date:
            meta_bits.append(_esc(date))
        if ref.commit_count > 0:
            meta_bits.append(f"{ref.commit_count} 个提交")
        meta_html = " ".join(f'<span class="vline"></span>{bit}' for bit in meta_bits)
        rows.append(
            f'<div class="row"><div class="row-ver">{_esc(name)}</div>'
            f'<div class="row-sum">{_rich(ref.summary)}</div>'
            f'<div class="row-meta">{mark}{meta_html}</div></div>'
        )

    body = (
        '<div class="head"><div class="head-main"><div class="eyebrow">Changelog</div>'
        f'<div class="ver">Index</div>{_title_line(f"共 {total} 个版本")}'
        f"{_title_line(f'显示最近 {len(shown)} 个')}</div>"
        f'<div class="head-side"><div class="orb">{_orb_svg()}</div>'
        '<div class="slogan">更快<br>更准<br>更智能</div></div></div>'
        f'<div class="metabar"><span class="backlink" style="margin-left:0;">'
        "用 core更新记录 &lt;版本号&gt; 看单版</span></div>"
        f'<div class="cols"><div class="col">{"".join(rows[: len(rows) // 2])}</div>'
        f'<div class="col">{"".join(rows[len(rows) // 2 :])}</div></div>'
        f'<div class="foot">— {_esc(_SLOGAN)} —</div>'
    )
    return _document(body)


def build_empty_html(title: str, text: str) -> str:
    """取不到记录时的降级卡片，仍然是一张图而不是一句报错。"""
    body = (
        '<div class="head"><div class="head-main"><div class="eyebrow">Changelog</div>'
        f'<div class="ver">404</div>{_title_line(title)}</div></div>'
        f'<div class="empty"><div class="empty-title">{_esc(title)}</div>'
        f'<div class="empty-text">{_rich(text)}</div></div>'
        f'<div class="foot">— {_esc(_SLOGAN)} —</div>'
    )
    return _document(body)
