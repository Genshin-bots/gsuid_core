"""render_agent 的移动端组件库（``gs-`` 前缀 CSS）。

HTML 用到 ``gs-`` 类时由 ``_prepare_free_html`` 注入到文档最前，
agent 自写的 ``<style>`` 排在后面，同优先级时可直接覆盖。
主题换色板（``theme-*`` 类）；既支持 ``gs-page`` 组件拼装，也允许自由定制 CSS。
"""

import re

RENDER_KIT_MARK = "gs render kit"

RENDER_LAYOUTS: tuple[str, ...] = (
    "feature",
    "gallery",
    "magazine",
    "dashboard",
    "ranking",
    "compare",
    "timeline",
    "brief",
)

# 嵌图开启时，只有这些明确声明为图文画册的版式必须写进照片
PHOTO_LAYOUTS: frozenset[str] = frozenset({"feature", "gallery", "magazine"})

_CLASS_ATTR_RE = re.compile(r"""class\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_LAYOUT_CLASS_RE = re.compile(r"\blayout-([a-z]+)\b")

RENDER_KIT_THEMES: tuple[str, ...] = (
    "paper",
    "ocean",
    "forest",
    "sunset",
    "sakura",
    "violet",
    "slate",
    "ink",
    "obsidian",
)

_KIT_CLASS_RE = re.compile(r"""class\s*=\s*["'][^"']*\bgs-[a-z]""", re.IGNORECASE)

# Takumi 只在容器带 border-radius 时裁 overflow，且不支持 :first-child/:last-child；
# 分隔线统一用相邻兄弟 + 选择器，防直塞卡片的子项用 :not() 保底留白。
RENDER_KIT_CSS = """
/* gs render kit — mobile long-image components */
html,body{margin:0;padding:0;}
.gs-page{
  --gs-bg:#f7f5f0;--gs-surface:#ffffff;--gs-text:#262b33;--gs-title:#11151c;
  --gs-muted:#68707d;--gs-line:#e7e2d9;--gs-accent:#0f766e;--gs-accent-soft:#e2f3f0;
  --gs-accent-ink:#0b5952;--gs-hero-from:#0f766e;--gs-hero-to:#11423f;
  --gs-up:#dc2626;--gs-down:#059669;--gs-glow:rgba(15,118,110,0.14);
  width:100%;background:var(--gs-bg);color:var(--gs-text);
  font-family:"MiSans","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:18px;line-height:1.65;font-weight:400;padding:0 0 32px;
  display:flex;flex-direction:column;
}
.gs-page.theme-ocean{--gs-bg:#eef4f9;--gs-line:#dae5f0;--gs-accent:#0284c7;
  --gs-accent-soft:#e0f2fe;--gs-accent-ink:#0369a1;--gs-hero-from:#0284c7;--gs-hero-to:#0c4a6e;
  --gs-glow:rgba(2,132,199,0.14);}
.gs-page.theme-forest{--gs-bg:#eff5f0;--gs-line:#dce7dd;--gs-accent:#16803c;
  --gs-accent-soft:#dcfce7;--gs-accent-ink:#166534;--gs-hero-from:#15803d;--gs-hero-to:#14532d;
  --gs-glow:rgba(22,128,60,0.14);}
.gs-page.theme-sunset{--gs-bg:#fcf5ef;--gs-line:#f1e2d4;--gs-accent:#ea580c;
  --gs-accent-soft:#ffedd5;--gs-accent-ink:#c2410c;--gs-hero-from:#ea580c;--gs-hero-to:#9a3412;
  --gs-glow:rgba(234,88,12,0.14);}
.gs-page.theme-sakura{--gs-bg:#fcf2f6;--gs-line:#f2dde6;--gs-accent:#db2777;
  --gs-accent-soft:#fce7f3;--gs-accent-ink:#be185d;--gs-hero-from:#db2777;--gs-hero-to:#831843;
  --gs-glow:rgba(219,39,119,0.14);}
.gs-page.theme-violet{--gs-bg:#f4f2fb;--gs-line:#e4dff5;--gs-accent:#6d28d9;
  --gs-accent-soft:#ede9fe;--gs-accent-ink:#5b21b6;--gs-hero-from:#6d28d9;--gs-hero-to:#3b0764;
  --gs-glow:rgba(109,40,217,0.14);}
.gs-page.theme-slate{--gs-bg:#f1f5f9;--gs-line:#e2e8f0;--gs-accent:#2563eb;
  --gs-accent-soft:#dbeafe;--gs-accent-ink:#1d4ed8;--gs-hero-from:#1e293b;--gs-hero-to:#0f172a;
  --gs-glow:rgba(37,99,235,0.14);}
.gs-page.theme-ink{--gs-bg:#0b0f17;--gs-surface:#131b26;--gs-text:#cbd5e1;--gs-title:#f8fafc;
  --gs-muted:#94a3b8;--gs-line:#223044;--gs-accent:#38bdf8;--gs-accent-soft:#132a3e;
  --gs-accent-ink:#7dd3fc;--gs-hero-from:#162235;--gs-hero-to:#090d14;
  --gs-up:#fb7185;--gs-down:#34d399;--gs-glow:rgba(56,189,248,0.18);}
.gs-page.theme-obsidian{--gs-bg:#07090e;--gs-surface:#11151f;--gs-text:#d1d5db;--gs-title:#ffffff;
  --gs-muted:#8b95a5;--gs-line:#1f2737;--gs-accent:#22d3ee;--gs-accent-soft:#0f2933;
  --gs-accent-ink:#67e8f9;--gs-hero-from:#121928;--gs-hero-to:#07090e;
  --gs-up:#fb7185;--gs-down:#34d399;--gs-glow:rgba(34,211,238,0.22);}

.gs-page > :not(.gs-hero):not(.gs-band):not(.gs-stat-hero):not(.gs-split):not(.gs-section):not(.gs-footer):not(style){
  margin-left:24px;margin-right:24px;margin-top:20px;}

.gs-hero{position:relative;width:100%;height:320px;overflow:hidden;border-radius:0 0 28px 28px;
  background:var(--gs-hero-to);}
.gs-hero.short{height:240px;}
.gs-hero img,.gs-cover img,.gs-thumb img,.gs-avatar img{position:absolute;left:0;top:0;
  width:100%;height:100%;max-width:none;object-fit:cover;}
.gs-hero-mask{position:absolute;left:0;top:0;right:0;bottom:0;
  background:linear-gradient(180deg,rgba(0,0,0,0.04) 0%,rgba(0,0,0,0.18) 42%,rgba(0,0,0,0.78) 100%);}
.gs-hero-body{position:absolute;left:28px;right:28px;bottom:32px;display:flex;flex-direction:column;gap:10px;}
.gs-hero.plain{height:auto;padding:54px 28px 36px;
  background:linear-gradient(140deg,var(--gs-hero-from) 0%,var(--gs-hero-to) 100%);}
.gs-hero.plain .gs-hero-body{position:relative;left:0;right:0;bottom:0;}
.gs-eyebrow{font-size:15px;font-weight:600;letter-spacing:0.08em;color:rgba(255,255,255,0.86);}
.gs-title{font-size:40px;font-weight:700;line-height:1.22;color:#ffffff;}
.gs-subtitle{font-size:18px;line-height:1.58;color:rgba(255,255,255,0.9);}
.gs-hero .gs-chips{margin-top:6px;}
.gs-hero .gs-chip{background:rgba(255,255,255,0.18);color:#ffffff;}
.gs-hero.tall{height:500px;}

.gs-band,.gs-stat-hero{display:flex;flex-direction:column;gap:12px;padding:42px 28px 30px;
  border-radius:0 0 28px 28px;background:linear-gradient(140deg,var(--gs-hero-from) 0%,var(--gs-hero-to) 100%);}
.gs-band-row{display:flex;align-items:center;gap:16px;}
.gs-band .gs-chip,.gs-stat-hero .gs-chip{background:rgba(255,255,255,0.18);color:#ffffff;}
.gs-band .gs-chips,.gs-stat-hero .gs-chips{margin-top:6px;}
.gs-stat-row{display:flex;align-items:flex-end;flex-wrap:wrap;gap:18px;}
.gs-stat-val{font-size:68px;font-weight:700;line-height:1.02;color:#ffffff;}
.gs-stat-delta{font-size:26px;font-weight:650;line-height:1.3;padding-bottom:6px;color:#ffffff;}
.gs-stat-hero .gs-up{color:#fda4af;}
.gs-stat-hero .gs-down{color:#6ee7b7;}

.gs-hero-kpi{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:22px 24px;
  border-radius:22px;background:var(--gs-surface);border:1px solid var(--gs-line);}
.gs-big-num{font-size:74px;font-weight:700;line-height:1.0;color:var(--gs-title);}
.gs-big-unit{font-size:24px;font-weight:600;color:var(--gs-muted);margin-left:6px;}
.gs-delta-list{display:flex;flex-direction:column;gap:10px;flex:none;}
.gs-delta-item{display:flex;align-items:center;gap:10px;font-size:17px;color:var(--gs-muted);white-space:nowrap;}
.gs-delta-item b{font-size:21px;font-weight:700;color:var(--gs-title);}

.gs-split{display:flex;align-items:stretch;gap:20px;padding:30px 24px 6px;}
.gs-split-body{flex:1;min-width:0;display:flex;flex-direction:column;justify-content:center;gap:10px;}
.gs-split-media{position:relative;width:300px;height:300px;flex:none;border-radius:24px;overflow:hidden;
  background:var(--gs-accent-soft);}
.gs-split-media img{position:absolute;left:0;top:0;width:100%;height:100%;max-width:none;object-fit:cover;}
.gs-split .gs-eyebrow{color:var(--gs-accent);}
.gs-split .gs-title{color:var(--gs-title);}
.gs-split .gs-subtitle{color:var(--gs-muted);}

.gs-section{display:flex;flex-direction:column;gap:14px;padding:0 24px;margin-top:24px;}
.gs-section.tight{margin-top:18px;}
.gs-h2{display:flex;align-items:center;gap:12px;font-size:25px;font-weight:700;
  line-height:1.32;color:var(--gs-title);}
.gs-h2-no{display:flex;align-items:center;justify-content:center;width:38px;height:38px;
  min-width:38px;max-width:38px;flex:0 0 38px;
  border-radius:12px;background:var(--gs-accent);color:#ffffff;font-size:17px;font-weight:700;}
.gs-h2-ico{display:flex;align-items:center;justify-content:center;width:40px;height:40px;
  min-width:40px;max-width:40px;flex:0 0 40px;
  border-radius:12px;background:var(--gs-accent-soft);}
.gs-h2-ico img{width:24px;height:24px;}
.gs-lead{font-size:16.5px;line-height:1.6;color:var(--gs-muted);}

.gs-card{display:flex;flex-direction:column;background:var(--gs-surface);color:var(--gs-text);
  border-radius:20px;overflow:hidden;border:1px solid var(--gs-line);
  box-shadow:0 6px 20px rgba(15,23,42,0.06);}
.gs-card.focus,.gs-kpi.focus,.gs-tile.focus{border:1.5px solid var(--gs-accent);
  box-shadow:0 8px 26px var(--gs-glow);}
.gs-card-body{display:flex;flex-direction:column;gap:12px;padding:20px 22px;}
.gs-card > :not(.gs-cover):not(.gs-media):not(.gs-card-body):not(style){
  margin-left:22px;margin-right:22px;}
.gs-card > .gs-card-title,.gs-card > .svg-wrap,.gs-card > .gs-list,.gs-card > .gs-kv-list,.gs-card > .gs-text{
  margin-top:18px;margin-bottom:18px;}
.gs-card > .gs-card-title + .svg-wrap,.gs-card > .gs-card-title + .gs-list,
.gs-card > .gs-card-title + .gs-kv-list,.gs-card > .gs-card-title + .gs-text{
  margin-top:0;}
.gs-card-title{font-size:21px;font-weight:700;line-height:1.35;color:var(--gs-title);}
.gs-text{font-size:17.5px;line-height:1.68;color:var(--gs-text);}
.gs-meta{font-size:14.5px;line-height:1.55;color:var(--gs-muted);}

.gs-card + .gs-card,.gs-card + .gs-tip,.gs-tip + .gs-card,.gs-tip + .gs-tip,
.gs-kpis + .gs-card,.gs-card + .gs-kpis,.gs-grid + .gs-card,.gs-card + .gs-grid,.gs-grid + .gs-tip{
  margin-top:14px;}
.gs-section > .gs-card + .gs-card,.gs-section > .gs-card + .gs-tip,.gs-section > .gs-tip + .gs-card,
.gs-section > .gs-tip + .gs-tip,.gs-section > .gs-kpis + .gs-card,.gs-section > .gs-card + .gs-kpis,
.gs-section > .gs-grid + .gs-card,.gs-section > .gs-card + .gs-grid,.gs-section > .gs-grid + .gs-tip{
  margin-top:0;}

.gs-cover{position:relative;width:100%;height:200px;overflow:hidden;border-radius:2px;
  background:var(--gs-accent-soft);}
.gs-cover.sm{height:150px;}
.gs-cover.lg{height:260px;}
.gs-on-img{position:absolute;left:14px;top:14px;display:flex;gap:8px;}

.gs-media{display:flex;gap:16px;align-items:flex-start;padding:18px 20px;}
.gs-thumb{position:relative;width:136px;height:136px;flex:none;border-radius:14px;overflow:hidden;
  background:var(--gs-accent-soft);}
.gs-thumb.wide{width:180px;height:124px;}
.gs-media-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px;}
.gs-avatar{position:relative;width:64px;height:64px;flex:none;border-radius:32px;overflow:hidden;
  background:var(--gs-accent-soft);}
.gs-avatar.lg{width:96px;height:96px;border-radius:48px;}
.gs-thumb.sm{width:64px;height:64px;border-radius:12px;}

.gs-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;}
.gs-grid.cols-3{grid-template-columns:1fr 1fr 1fr;gap:12px;}
.gs-grid.cols-3 .gs-cover{height:120px;}
.gs-grid.cols-3 .gs-tile-title{font-size:17px;}
.gs-tile{display:flex;flex-direction:column;min-width:0;background:var(--gs-surface);border-radius:16px;
  overflow:hidden;border:1px solid var(--gs-line);}
.gs-tile .gs-cover{height:140px;}
.gs-tile-body{display:flex;flex-direction:column;gap:6px;padding:14px 16px 16px;}
.gs-tile-title{font-size:18.5px;font-weight:650;line-height:1.35;color:var(--gs-title);}

.gs-chips{display:flex;flex-wrap:wrap;gap:8px;}
.gs-chip{display:flex;align-items:center;justify-content:center;gap:6px;padding:5px 12px;border-radius:999px;
  background:var(--gs-accent-soft);color:var(--gs-accent-ink);font-size:14.5px;font-weight:600;
  line-height:1.25;white-space:nowrap;word-break:keep-all;flex-shrink:0;}
.gs-chip img{width:15px;height:15px;}
.gs-chip.solid{background:var(--gs-accent);color:#ffffff;}
.gs-chip.ghost{background:var(--gs-surface);border:1px solid var(--gs-line);color:var(--gs-muted);}

.gs-kpis{display:flex;gap:12px;}
.gs-kpi{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px;padding:16px 18px;border-radius:18px;
  background:var(--gs-surface);border:1px solid var(--gs-line);}
.gs-kpi-head{display:flex;align-items:center;gap:8px;font-size:14.5px;font-weight:600;color:var(--gs-muted);}
.gs-kpi-head img{width:18px;height:18px;flex:none;}
.gs-kpi-val{font-size:32px;font-weight:700;line-height:1.12;color:var(--gs-title);}
.gs-kpi-lab{font-size:14.5px;line-height:1.4;color:var(--gs-muted);}

.gs-timeline{display:flex;flex-direction:column;}
.gs-tl{display:flex;gap:14px;}
.gs-tl-axis{width:18px;flex:none;display:flex;flex-direction:column;align-items:center;}
.gs-tl-dot{width:14px;height:14px;flex:none;border-radius:7px;margin-top:7px;background:var(--gs-accent);}
.gs-tl-line{width:2px;flex:1;margin-top:6px;background:var(--gs-line);}
.gs-tl-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:8px;padding-bottom:22px;}
.gs-tl-time{font-size:15px;font-weight:700;letter-spacing:0.04em;color:var(--gs-accent);}

.gs-list{display:flex;flex-direction:column;gap:10px;}
.gs-li{display:flex;gap:12px;align-items:flex-start;font-size:17.5px;line-height:1.62;}
.gs-li-dot{width:8px;height:8px;flex:none;border-radius:4px;margin-top:11px;background:var(--gs-accent);}
.gs-kv-list{display:flex;flex-direction:column;}
.gs-kv{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;padding:13px 0;
  font-size:17px;line-height:1.55;}
.gs-kv + .gs-kv{border-top:1px solid var(--gs-line);}
.gs-k{flex:none;white-space:nowrap;word-break:keep-all;color:var(--gs-muted);}
.gs-v{flex:1;min-width:0;text-align:right;font-weight:600;color:var(--gs-text);}

.gs-tip{display:flex;gap:12px;align-items:flex-start;padding:16px 20px;border-radius:16px;
  background:var(--gs-accent-soft);color:var(--gs-accent-ink);font-size:17px;line-height:1.62;}
.gs-tip.stack{flex-direction:column;gap:8px;}
.gs-ico{width:26px;height:26px;min-width:26px;max-width:26px;flex:0 0 26px;}
.gs-ico.lg{width:34px;height:34px;min-width:34px;max-width:34px;flex:0 0 34px;}
.gs-ico img{width:100%;height:100%;}
.gs-tip .gs-ico{margin-top:2px;}

.gs-rank{display:flex;flex-direction:column;background:var(--gs-surface);border-radius:20px;
  border:1px solid var(--gs-line);overflow:hidden;}
.gs-rank-row{display:flex;align-items:center;gap:14px;padding:14px 20px;}
.gs-rank-row + .gs-rank-row{border-top:1px solid var(--gs-line);}
.gs-rank-row.top{background:var(--gs-accent-soft);}
.gs-rank-no{display:flex;align-items:center;justify-content:center;width:40px;height:40px;flex:none;
  border-radius:12px;background:var(--gs-bg);color:var(--gs-muted);font-size:18px;font-weight:700;}
.gs-rank-row.top .gs-rank-no{background:var(--gs-accent);color:#ffffff;}
.gs-rank-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px;}
.gs-rank-name{font-size:18.5px;font-weight:650;line-height:1.35;color:var(--gs-title);}
.gs-rank-val{flex:none;font-size:21px;font-weight:700;line-height:1.2;color:var(--gs-title);text-align:right;}

.gs-compare-head{display:flex;align-items:center;gap:12px;margin-top:8px;}
.gs-compare-side{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:8px;
  padding:16px 12px;border-radius:20px;background:rgba(255,255,255,0.14);}
.gs-compare-name{font-size:22px;font-weight:700;line-height:1.3;color:#ffffff;text-align:center;}
.gs-compare-side .gs-meta{color:rgba(255,255,255,0.82);text-align:center;}
.gs-compare-vs{flex:none;font-size:24px;font-weight:700;color:#ffffff;}
.gs-compare-table{display:flex;flex-direction:column;background:var(--gs-surface);border-radius:20px;
  border:1px solid var(--gs-line);overflow:hidden;}
.gs-compare-row{display:flex;align-items:center;gap:10px;padding:13px 18px;
  font-size:17.5px;line-height:1.45;}
.gs-compare-row + .gs-compare-row{border-top:1px solid var(--gs-line);}
.gs-compare-a,.gs-compare-b{flex:1;min-width:0;color:var(--gs-text);}
.gs-compare-a{text-align:right;}
.gs-compare-b{text-align:left;}
.gs-compare-k{width:140px;flex:none;text-align:center;font-size:15px;color:var(--gs-muted);}
.gs-compare-a.win,.gs-compare-b.win{font-weight:700;color:var(--gs-accent);}

.gs-quote{display:flex;flex-direction:column;gap:10px;padding:22px 24px;border-radius:20px;
  background:var(--gs-accent-soft);}
.gs-quote-text{font-size:23px;font-weight:650;line-height:1.55;color:var(--gs-accent-ink);}
.gs-quote-by{font-size:15px;line-height:1.5;color:var(--gs-muted);text-align:right;}

.gs-steps{display:flex;flex-direction:column;gap:12px;}
.gs-step{display:flex;gap:14px;align-items:flex-start;padding:16px 20px;border-radius:18px;
  background:var(--gs-surface);border:1px solid var(--gs-line);}
.gs-step-no{display:flex;align-items:center;justify-content:center;width:36px;height:36px;flex:none;
  border-radius:18px;background:var(--gs-accent);color:#ffffff;font-size:17px;font-weight:700;}
.gs-step-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px;}

.gs-row{display:flex;gap:12px;align-items:center;}
.gs-row.top{align-items:flex-start;}
.gs-col{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px;}
.gs-accent{color:var(--gs-accent);}
.gs-b{font-weight:650;color:var(--gs-title);}
.gs-up{color:var(--gs-up);}
.gs-down{color:var(--gs-down);}
.gs-footer{margin:34px 24px 0;padding-top:16px;border-top:1px solid var(--gs-line);
  font-size:14.5px;line-height:1.6;color:var(--gs-muted);}
.gs-page pre{padding:14px 16px;border-radius:14px;background:var(--gs-bg);border:1px solid var(--gs-line);
  color:var(--gs-text);font-family:"Mono",Consolas,Menlo,monospace;font-size:15px;line-height:1.58;white-space:pre-wrap;}
.gs-page code{font-family:"Mono",Consolas,Menlo,monospace;font-size:0.92em;}
"""


def uses_render_kit(html: str) -> bool:
    return _KIT_CLASS_RE.search(html) is not None


def page_layout(html: str) -> str | None:
    """``gs-page`` 上声明的版式名；没声明或不在目录里返回 None。"""
    for match in _CLASS_ATTR_RE.finditer(html):
        classes = match.group(1).split()
        if "gs-page" not in classes:
            continue
        found = _LAYOUT_CLASS_RE.search(" ".join(classes))
        if found is not None and found.group(1) in RENDER_LAYOUTS:
            return found.group(1)
        return None
    return None


def inject_render_kit(html: str) -> str:
    """把组件库 CSS 放在 agent 样式之前；没用 ``gs-`` 类或已注入则原样返回。"""
    if RENDER_KIT_MARK in html or not uses_render_kit(html):
        return html
    block = f"<style>\n{RENDER_KIT_CSS}\n</style>\n"
    lower = html.lower()
    head = re.search(r"<head\b[^>]*>", lower)
    if head is not None:
        return html[: head.end()] + "\n" + block + html[head.end() :]
    first_style = lower.find("<style")
    if first_style != -1:
        return html[:first_style] + block + html[first_style:]
    return block + html
