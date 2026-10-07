"""更新记录卡片模板：把结构化记录拼成整页 HTML。

版式照参考图来：左上超大版本号 + 标题两行，右侧轨道球装饰，底下一条信息带，
再一块细线引言面板（右侧带迷你指标卡），条目走**两栏**排，底部居中标语。

前几版栽在同一个地方：把内容塞进一堆圆角面板。这版只在引言处留一块细线面板，
其余全靠留白、字重和一根竖线分栏。底色是近乎纯黑 + 右上暖光，不是星空壁纸。
"""

from __future__ import annotations

import re
import html
import base64
import random
from typing import Sequence
from pathlib import Path

from gsuid_core.utils.image.image_tools import TEXT_PATH

from .changelog import (
    VersionRef,
    CommitAuthor,
    ChangelogEntry,
    ChangelogGroup,
    ChangelogVersion,
    peek_emoji,
)

# 逻辑宽（CSS px）。设备像素 = 本值 * DEVICE_SCALE，渲染时 max_width / dpi 同步翻倍。
LAYOUT_WIDTH = 560
DEVICE_SCALE = 2

_MAX_COMMITS = 8
_INDEX_LIMIT = 12

_STAR_W = LAYOUT_WIDTH
_STAR_H = 9600
_STAR_SEED = 7
_STAR_COUNT = 190
# 右上角光团。此前是暖黄 #7a5a22，改红后与橙色标题撞色，故降透明度避免糊成一片。
_GLOW_HEX = "#7a1f22"

# 金调是这套版式的主色，版本号用奶油白，标题与指标用金
_CREAM = "#efe3cd"
_GOLD = "#e0ae4c"
_GOLD_SOFT = "#c99a45"
_ORANGE = "#f08a3c"
_MUTED = "#8a8a96"

# 仓库 commit emoji 约定里没有 icon 的类别，给个中性点
_FALLBACK_ICON = "◆"
_SLOGAN = "让智能更有温度"

# 「重要」的定义：带单位的量化事实。版本号 0.11.0 不会被误命中（后面跟的是点不是单位）。
_HL_UNITS = "倍|条|次|个|万|亿|%|ms|s|x|X|秒|分钟|小时|天|年|人|项|处|版|款|台|档"
_HL_RE = re.compile(rf"\d+(?:\.\d+)?\s*(?:{_HL_UNITS})(?![a-zA-Z])")
_CODE_SPLIT_RE = re.compile(r"(`[^`]+`)")

# 关键词染橙：读者靠这些词一眼看出「这条改了什么动作」。只收技术动作词，
# 收「的」「是」这类虚词会把整句染花。正则按长度倒序，长词先匹配。
_KEYWORDS = (
    "线程池",
    "断线重连",
    "内存泄漏",
    "回归",
    "降级",
    "熔断",
    "限流",
    "并发",
    "异步",
    "同步",
    "超时",
    "重试",
    "鉴权",
    "缓存",
    "队列",
    "索引",
    "分页",
    "懒加载",
    "预加载",
    "热重载",
    "持久化",
    "事务",
    "幂等",
    "自愈",
    "死锁",
    "竞态",
    "兜底",
    "中断",
    "截断",
    "对齐",
    "落地",
    "废弃",
    "兼容",
    "上线",
    "提速",
    "开销",
    "泄漏",
    "崩溃",
    "卡死",
)
_KW_RE = re.compile("|".join(sorted(_KEYWORDS, key=len, reverse=True)))


def _esc(text: str) -> str:
    return html.escape(text, quote=False)


def _esc_attr(text: str) -> str:
    return html.escape(text, quote=True)


def _accent_hex(color: str) -> str:
    """`_CATEGORY_EMOJI` 里存的是不带 `#` 的裸十六进制（如 `4ade80`）。

    直接塞进 `color:` 会变成 `color:4ade80` 这种非法值，渲染器整个丢弃，
    文字退回默认色——badge 会看着像没上色。拼上 `#` 才是合法 CSS。
    """
    return color if color.startswith("#") else f"#{color}"


def _rgba(color: str, alpha: float) -> str:
    value = color.lstrip("#")
    return f"rgba({int(value[0:2], 16)},{int(value[2:4], 16)},{int(value[4:6], 16)},{alpha})"


def _icon_box(inner: str, size: int) -> str:
    """用外层容器定尺寸。

    实测：pytakumi 下 inline <svg> 的 width/height 属性与 CSS width 一律失效，
    会被拉到整行宽（声明 8~96 全部渲染成 122 CSS px），必须由父容器约束。
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


_LOGO_SIZE = 82
# 项目正式 ICON：help/utils.py 的 register_help 默认就用仓库根目录这张
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_ICON_CANDIDATES = (_PROJECT_ROOT / "ICON.png", _PROJECT_ROOT / "gsuid_core" / "webstatic" / "ICON.png")
# 卡片是深底，footer 取 help/texture2d 的 dark 版（浅色文字），light 版是配白底的
_FOOTER_CANDIDATES = (
    _PROJECT_ROOT / "gsuid_core" / "help" / "texture2d" / "footer_dark.png",
    TEXT_PATH / "footer.png",
)


def _read_png(candidates: Sequence[Path]) -> bytes:
    """按顺序取第一个能读到的资源，全缺则返回 b""，让调用方走兜底。"""
    for path in candidates:
        try:
            return path.read_bytes()
        except OSError:
            continue
    return b""


def _logo_uri() -> str:
    """右上角标志用项目正式的 `ICON.png`（Sayu 形象），不另找功能图标。"""
    raw = _read_png(_ICON_CANDIDATES)
    if not raw:
        return ""
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


_LOGO_URI = _logo_uri()


def _logo() -> str:
    """右上角图标。外层 span 定尺寸，同 _icon_box 的坑：img 自身宽高在此渲染器不可靠。"""
    if _LOGO_URI:
        inner = f'<img src="{_LOGO_URI}" alt="更新记录">'
    else:
        inner = '<span class="logo-fallback">LOG</span>'
    return f'<span class="orb" style="width:{_LOGO_SIZE}px;height:{_LOGO_SIZE}px">{inner}</span>'


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
            f'rx="{int(260 * ratio)}" ry="{int(220 * ratio)}" fill="{_GLOW_HEX}" opacity="0.042"/>'
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


def _footer_uri() -> str:
    """底部署名用核心现成的 footer 资源，不自己编标语。

    优先 `help/texture2d/footer_dark.png`：卡片是近黑底，dark 版是浅色文字；
    light 版是深色文字，贴上来会糊在底里。全缺则回落空串，模板改用文字署名。
    """
    raw = _read_png(_FOOTER_CANDIDATES)
    if not raw:
        return ""
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


_FOOTER_URI = _footer_uri()

_CSS = (
    """*{margin:0;padding:0;}
body{
  width:{LAYOUT_WIDTH}px;overflow:hidden;
  background-image:url("__STARFIELD__");
  background-repeat:repeat-y;background-size:{LAYOUT_WIDTH}px 9600px;background-color:#07070c;
  font-family:"MiSans","PingFang SC","Microsoft YaHei",sans-serif;
  color:#f0f0f2;line-height:1.5;padding:24px 22px 20px 22px;
}
/* min-height 只保证放得下 82px 的头像组；原先 152px 是给 150px 轨道球留的位，
   头像缩小后这行下限过期，短标题版本下方会空出一大块。标题行数不同的高度交给内容撑。 */
.head{position:relative;min-height:86px;}
.head-main{flex:1;}
/* 图标与标语并排：此前两者各自绝对定位到右上，头像一大就压住标语。
   整组右收 8px，别贴死右边缘（否则标语看着像被裁），并垂直居中贴住标题区，
   短标题时右侧不再空出一大块。 */
.head-side{position:absolute;top:0;right:8px;bottom:0;display:flex;align-items:center;
  gap:13px;pointer-events:none;}
.orb{display:block;overflow:hidden;flex:none;}
/* 尺寸由外层 .orb 的内联样式给；这里只保证 img 填满且不被渲染器按原图 256px 摆 */
.orb img{display:block;width:100%;height:100%;}
.logo-fallback{display:block;width:100%;height:100%;border:1px solid rgba(224,174,76,0.5);
  border-radius:10px;font-size:13px;font-weight:700;color:{GOLD_SOFT};text-align:center;
  line-height:80px;letter-spacing:0.1em;}
.slogan{font-size:12px;font-weight:600;color:#8d8d99;line-height:1.75;text-align:right;
  white-space:nowrap;letter-spacing:0.06em;flex:none;}
.eyebrow{font-size:9.5px;font-weight:700;letter-spacing:0.42em;color:{GOLD};text-transform:uppercase;}
.ver{font-size:52px;font-weight:600;line-height:1.02;letter-spacing:1px;color:{CREAM};margin-top:8px;}
/* 标题：橙金 + 斜体。实测本渲染器不认 transform（skewX/rotate 全部无效），
   倾斜只能用 font-style:italic，斜度固定在字体自带角度、调不了。 */
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
.lead{display:flex;margin-top:13px;padding:11px 13px 12px 11px;border-radius:10px;
  border:1px solid rgba(255,255,255,0.10);background:rgba(255,255,255,0.022);}
.quote{font-size:26px;font-weight:700;color:#5b8def;line-height:1;margin-right:9px;}
.lead-main{flex:1;}
.lead-hl{font-size:14px;font-weight:700;color:#f4f4f6;line-height:1.45;}
.lead-rest{margin-top:4px;font-size:11px;color:#8e8e9a;line-height:1.58;}
/* 单列分组：一组 = emoji + 粗分类胶囊 + 项数，组内子条目依次列出 */
.groups{margin-top:14px;}
.group{margin-bottom:14px;}
.group:last-child{margin-bottom:0;}
.ghead{display:flex;align-items:center;}
.ghead-body{display:flex;align-items:center;}
.gcount{margin-left:6px;padding:2px 8px;border-radius:9px;font-size:10px;font-weight:700;
  letter-spacing:0.04em;white-space:nowrap;}
.gitems{margin-top:6px;padding-left:25px;}
/* 段间距要明显大于换行行距，否则多行条目和下一条会粘成一块。 */
.item{margin-top:9px;padding:2px 0 2px 8px;border-left:1px solid rgba(255,255,255,0.18);}
.item:first-child{margin-top:0;}
.ico{width:25px;flex:none;font-size:17px;line-height:1.3;}
/* 分类标签走胶囊：圆角取到约半高，两端收成半圆 */
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:10.5px;font-weight:700;
  border:1px solid transparent;white-space:nowrap;word-break:keep-all;letter-spacing:0.02em;}
.txt{font-size:11px;font-weight:500;color:#e8e8ec;line-height:1.36;}
.meta{font-family:"Mono","Consolas",monospace;font-size:8.5px;font-weight:400;
  color:#6b6b78;line-height:1.36;}
.sha{font-weight:400;letter-spacing:0.02em;}
.hl{color:{GOLD};font-weight:600;}
.kw{color:{ORANGE};font-weight:600;}
/* 标题底色已是橙色，标题内的标记改奶油白提亮 */
.title .hl,.title .kw{color:{CREAM};font-weight:700;}
.code{font-family:"Mono","Consolas",monospace;font-size:10.5px;padding:0 3px;border-radius:3px;
  background:rgba(91,141,239,0.13);color:#7fb0ff;font-weight:600;}
.avs{display:inline-block;line-height:0;vertical-align:-1px;}
.av{display:inline-block;overflow:hidden;border-radius:50%;
  border:1px solid rgba(255,255,255,0.22);background:#16161c;line-height:0;
  vertical-align:middle;}
.av+.av{margin-left:-4px;}
.av img{display:block;width:100%;height:100%;}
.av-letter{display:block;width:100%;height:100%;text-align:center;
  font-size:8px;font-weight:700;color:#c9c9d4;}
.contrib{display:flex;align-items:center;margin-top:8px;}
.contrib-lb{margin-left:8px;font-size:10px;color:#7a7a86;letter-spacing:0.04em;}
.rows{margin-top:14px;}
/* 合集卡：一段一版。段头用中等字号，两版并排时不会和单版卡的超大号打架 */
.segments{margin-top:16px;}
.segment+.segment{margin-top:20px;padding-top:18px;border-top:1px solid rgba(255,255,255,0.10);}
.seg-head{display:flex;align-items:baseline;gap:8px;}
.seg-ver{font-size:26px;font-weight:600;line-height:1.1;letter-spacing:0.5px;color:{CREAM};}
.seg-flag{padding:2px 8px;border-radius:9px;font-size:9.5px;font-weight:700;letter-spacing:0.06em;
  color:{ORANGE};background:rgba(240,138,60,0.15);}
.seg-meta{display:flex;align-items:center;margin-top:5px;font-size:9.5px;color:#7a7a86;}
.seg-vline{width:1px;height:9px;background:rgba(255,255,255,0.14);margin:0 8px;}
.seg-vline:first-child{margin-left:0;}
.seg-sub{margin-top:7px;font-size:11.5px;color:#b9b9c4;line-height:1.55;}
/* 索引行：左侧 emoji 作时间线节点（圆底遮住竖线）+ 正文两行（版本行 / 摘要）。
   竖线绝对定位在 .rows 上，top/bottom 各让 21px，使线段正好落在首尾节点的圆心。
   行间不再画横线，竖线本身就是分隔。 */
.rows{margin-top:14px;position:relative;}
.tline{position:absolute;left:13px;top:21px;bottom:21px;width:1px;background:rgba(255,255,255,0.10);}
.row{padding:7px 0;display:flex;align-items:center;}
.row+.row{padding-top:16px;}
.row-emoji{width:28px;height:28px;flex:none;font-size:16px;line-height:28px;text-align:center;
  border-radius:50%;background:#07070c;}
.row-body{flex:1;min-width:0;margin-left:14px;}
.row-head{display:flex;align-items:center;flex-wrap:wrap;}
.row-ver{font-size:12.5px;font-weight:700;color:#f0f0f2;line-height:1.32;}
.row-date{margin-left:8px;font-size:9px;color:#7a7a86;letter-spacing:0.04em;}
.row-commits{margin-left:8px;font-size:9px;color:#6b6b78;letter-spacing:0.04em;}
.row-sum{margin-top:3px;font-size:10px;color:#9a9aa6;line-height:1.5;}
.unreleased{margin-left:7px;padding:1px 6px;border-radius:8px;font-size:8.5px;font-weight:700;
  color:{GOLD};background:rgba(224,174,76,0.16);white-space:nowrap;}
.empty{margin-top:16px;}
.empty-title{font-size:14px;font-weight:600;color:#f0f0f2;}
.empty-text{margin-top:8px;font-size:11px;color:#8e8e9a;line-height:1.85;}
.foot{margin-top:16px;text-align:center;font-size:10px;color:#5d5d69;letter-spacing:0.12em;}
.foot img{display:block;width:300px;margin:0 auto;opacity:0.42;image-rendering:auto;}""".replace(
        "{GOLD_SOFT}", _GOLD_SOFT
    )
    .replace("{CREAM}", _CREAM)
    .replace("{GOLD}", _GOLD)
    .replace("{ORANGE}", _ORANGE)
    .replace("{LAYOUT_WIDTH}", str(LAYOUT_WIDTH))
)


def _highlight(segment: str) -> str:
    """量化事实染金 + 关键词染橙。关键词规则要放在量化规则之后，
    否则 `3 个` 里的「个」不会被当成关键词重复染色。"""
    out = _HL_RE.sub(lambda matched: f'<span class="hl">{matched.group(0)}</span>', segment)
    return _KW_RE.sub(lambda matched: f'<span class="kw">{matched.group(0)}</span>', out)


def _rich(text: str) -> str:
    """转义 → 反引号片段转等宽样式 → 其余文字高亮量化事实与关键词。

    顺序不能换：高亮只在非代码片段上跑，否则 `0.10.8` 这类标识符会被涂色。
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
        f'<div class="head-side">{_logo()}'
        f'<div class="slogan">更快<br>更准<br>更智能</div></div></div>'
    )


def _meta_item(icon: str, label: str) -> str:
    return f'<span class="mi">{icon}<span class="lb">{label}</span></span>'


def _footer() -> str:
    """底部署名：优先用核心现成的 footer.png，缺资源才退回文字。"""
    if _FOOTER_URI:
        return f'<div class="foot"><img src="{_FOOTER_URI}" alt="Created by GsCore"></div>'
    return f'<div class="foot">— {_esc(_SLOGAN)} —</div>'


def _meta_bar(version: ChangelogVersion) -> str:
    items: list[str] = []
    if version.date:
        items.append(_meta_item(_icon_calendar(_MUTED), f"发布于 {version.date}"))
        items.append('<span class="vline"></span>')
    if version.commit_count > 0:
        items.append(_meta_item(_icon_version(_MUTED), f"本版 {version.commit_count} 个提交"))
        items.append('<span class="vline"></span>')
    items.append(_meta_item(_icon_code(_MUTED), "提交范围见 pyproject.toml"))
    bar = f'<div class="metabar">{"".join(items)}<span class="backlink">&lt; 返回索引</span></div>'
    return bar + _contributors_html(version.authors)


def _group_html(group: ChangelogGroup) -> str:
    """一组 = emoji + 分类 badge + 项数 badge，组内子条目依次列出。

    badge 用实心淡底 + 同色描边，比原先的纯色胶囊更像「标签」而不是按钮。
    """
    count = len(group.items)
    accent = _accent_hex(group.accent)
    counter = (
        f'<span class="gcount" style="background:{_rgba(group.accent, 0.15)};color:{accent};">{count} 项</span>'
        if count > 1
        else ""
    )
    badge = (
        f'<span class="badge" style="background:{_rgba(group.accent, 0.16)};'
        f'border-color:{_rgba(group.accent, 0.55)};color:{accent};">{_esc(group.label)}</span>'
    )
    items = "".join(_item_html(item) for item in group.items)
    return (
        f'<div class="group"><div class="ghead"><div class="ico">{group.emoji}</div>'
        f'<div class="ghead-body">{badge}{counter}</div></div>'
        f'<div class="gitems">{items}</div></div>'
    )


def _avatar_html(author: CommitAuthor, size: int) -> str:
    """外层 span 定尺寸。pytakumi 下 img 自身宽高不可靠，同 _icon_box。"""
    label = author.login or author.name or "?"
    title = _esc_attr(label)
    if author.avatar_uri:
        inner = f'<img src="{author.avatar_uri}" alt="{title}">'
    else:
        letter = _esc(label[:1].upper())
        inner = f'<span class="av-letter" style="line-height:{size}px">{letter}</span>'
    return f'<span class="av" style="width:{size}px;height:{size}px" title="{title}">{inner}</span>'


def _avatars_html(authors: Sequence[CommitAuthor], size: int = 18) -> str:
    if not authors:
        return ""
    return f'<span class="avs">{"".join(_avatar_html(a, size) for a in authors)}</span>'


def _contributors_html(authors: Sequence[CommitAuthor]) -> str:
    if not authors:
        return ""
    count = len(authors)
    return f'<div class="contrib">{_avatars_html(authors, 20)}<span class="contrib-lb">{count} 位贡献者</span></div>'


def _item_html(entry: ChangelogEntry) -> str:
    """组内子条目：正文后用 `| ` 接作者头像和 commit 短号，不再单独占一行。"""
    commits = entry.commits[:_MAX_COMMITS]
    rest = len(entry.commits) - len(commits)
    sha_text = " · ".join(commits)
    if rest > 0:
        sha_text += f" · +{rest}"
    txt = f'<span class="txt">{_rich(entry.text)}</span>' if entry.text else ""
    bits: list[str] = []
    avatars = _avatars_html(entry.authors, 13)
    if avatars:
        bits.append(avatars)
    if sha_text:
        bits.append(f'<span class="sha">{_esc(sha_text)}</span>')
    if not bits:
        return f'<div class="item">{txt}</div>'
    meta = f'<span class="meta"> | {" ".join(bits)}</span>'
    return f'<div class="item">{txt}{meta}</div>'


def _lead_block(version: ChangelogVersion) -> str:
    """引言面板。此前右侧挂过迷你指标卡 + 上升折线，读着像无关的装饰图例，
    且指标是从正文正则抓的、并非该版真实 KPI，已整块去掉。"""
    if not version.lead:
        return ""
    first = version.lead[0]
    head, sep, tail = first.partition("。")
    lead_hl = f"{head}。" if sep else first
    rest_blocks = list(version.lead[1:])
    if tail.strip():
        rest_blocks.insert(0, tail.strip())
    lead_rest = f'<div class="lead-rest">{_rich(" ".join(rest_blocks))}</div>' if rest_blocks else ""
    return (
        f'<div class="lead"><div class="quote">❝</div><div class="lead-main">'
        f'<div class="lead-hl">{_rich(lead_hl)}</div>{lead_rest}</div></div>'
    )


def _split_subtitle(subtitle: str) -> tuple[str, str]:
    """`消息吞吐最优 3.3x，记忆注入预算统一` → 首句 + 余下，与参考图的两行标题一致。"""
    head, sep, tail = subtitle.partition("，")
    if not sep or not tail.strip():
        return subtitle, ""
    return head, tail


def _segment_html(version: ChangelogVersion) -> str:
    """合集卡里的一「段」：一个小版本头 + 该版的引言与条目。

    单版卡的头部是超大版本号；合集里两版并排会打架，所以段头改用中等字号，
    版本号与「未发布」标记同行，读者靠它分隔两段。
    """
    mark = "" if version.released else '<span class="seg-flag">未发布</span>'
    meta_bits: list[str] = []
    if version.date:
        meta_bits.append(_esc(_index_date_of(version.date, version.released)))
    if version.commit_count > 0:
        meta_bits.append(f"{version.commit_count} 个提交")
    meta_html = "".join(f'<span class="seg-vline"></span>{bit}' for bit in meta_bits)
    return (
        f'<div class="segment"><div class="seg-head"><div class="seg-ver">{_esc(version.version)}</div>'
        f'{mark}</div><div class="seg-meta">{meta_html}</div>'
        f'<div class="seg-sub">{_rich(version.subtitle)}</div>'
        f"{_contributors_html(version.authors)}"
        f'{_lead_block(version)}<div class="groups">'
        f"{''.join(_group_html(g) for g in version.groups)}</div></div>"
    )


def _index_date_of(date: str, released: bool) -> str:
    """合集段头复用索引的去重规则：未发布段的日期别再带「（未发布）」。"""
    if released:
        return date
    return date.replace("（未发布）", "").strip()


def build_recent_html(versions: Sequence[ChangelogVersion]) -> str:
    """默认那张图：当前版本；提交不超过 6 个时再拼上一版。

    调用方保证非空；空列表请自己走 `build_empty_html`。
    """
    blocks = [_segment_html(v) for v in versions]
    labels = " + ".join(f"{v.version}{'' if v.released else ' 未发布'}" for v in versions)
    body = (
        '<div class="head"><div class="head-main"><div class="eyebrow">Changelog</div>'
        f'<div class="ver">Recent</div>{_title_line(f"最近 {len(versions)} 段改动")}'
        f"{_title_line(labels)}</div>"
        f'<div class="head-side">{_logo()}'
        '<div class="slogan">更快<br>更准<br>更智能</div></div></div>'
        f'<div class="metabar"><span class="backlink" style="margin-left:0;">'
        "用 core更新记录 &lt;版本号&gt; 看单版 · 列表看全部</span></div>"
        f'<div class="segments">{"".join(blocks)}</div>'
        f"{_footer()}"
    )
    return _document(body)


def build_version_html(version: ChangelogVersion, *, is_current: bool) -> str:
    """单版本卡片。`is_current` 只影响信息带右侧的版本态标记。"""
    body = (
        f"{_title_block(version)}{_meta_bar(version)}{_lead_block(version)}"
        f'<div class="groups">{"".join(_group_html(g) for g in version.groups)}</div>'
        f"{_footer()}"
    )
    return _document(body)


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
        date = _index_date(ref)
        # 日期跟着版本号走同一基线，提交数留在摘要下方那行
        head_bits = [f'<span class="row-ver">{_esc(name)}</span>']
        if mark:
            head_bits.append(mark)
        if date:
            head_bits.append(f'<span class="row-date">{_esc(date)}</span>')
        if ref.commit_count > 0:
            head_bits.append(f'<span class="row-commits">{ref.commit_count} 个提交</span>')
        rows.append(
            f'<div class="row"><div class="row-emoji">{peek_emoji(ref)}</div>'
            f'<div class="row-body"><div class="row-head">{"".join(head_bits)}</div>'
            f'<div class="row-sum">{_rich(ref.summary)}</div></div></div>'
        )

    body = (
        '<div class="head"><div class="head-main"><div class="eyebrow">Changelog</div>'
        f'<div class="ver">Index</div>{_title_line(f"共 {total} 个版本")}'
        f"{_title_line(f'显示最近 {len(shown)} 个')}</div>"
        f'<div class="head-side">{_logo()}'
        '<div class="slogan">更快<br>更准<br>更智能</div></div></div>'
        f'<div class="metabar"><span class="backlink" style="margin-left:0;">'
        "用 core更新记录 &lt;版本号&gt; 看单版</span></div>"
        f'<div class="rows"><div class="tline"></div>{"".join(rows)}</div>'
        f"{_footer()}"
    )
    return _document(body)


def build_empty_html(title: str, text: str) -> str:
    """取不到记录时的降级卡片，仍然是一张图而不是一句报错。"""
    body = (
        '<div class="head"><div class="head-main"><div class="eyebrow">Changelog</div>'
        f'<div class="ver">404</div>{_title_line(title)}</div></div>'
        f'<div class="empty"><div class="empty-title">{_esc(title)}</div>'
        f'<div class="empty-text">{_rich(text)}</div></div>'
        f"{_footer()}"
    )
    return _document(body)
