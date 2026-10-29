"""render_agent 的移动端组件库（``gs-`` 前缀 CSS）。

HTML 用到 ``gs-`` 类时由 ``_prepare_free_html`` 注入到文档最前，
agent 自写的 ``<style>`` 排在后面，同优先级时可直接覆盖。
主题只换色板（``theme-*`` 类）；版式由 ``gs-page`` 上的 ``layout-*`` 声明，组件拼装。
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

# 嵌图开启时，只有这些版式必须写进照片
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
)

_KIT_CLASS_RE = re.compile(r"""class\s*=\s*["'][^"']*\bgs-[a-z]""", re.IGNORECASE)

# Takumi 只在容器带 border-radius 时裁 overflow，有绝对定位兄弟时 img 的 height:100% 失效；
# 所以图片槽位都带圆角、img 绝对铺满。background-image 会画出容器外，不用它放照片。
RENDER_KIT_CSS = """
/* gs render kit — mobile long-image components */
html,body{margin:0;padding:0;}
.gs-page{
  --gs-bg:#f6f3ee;--gs-surface:#ffffff;--gs-text:#2a2f36;--gs-title:#15191e;
  --gs-muted:#7a818c;--gs-line:#ebe6de;--gs-accent:#0f766e;--gs-accent-soft:#e3f1ee;
  --gs-accent-ink:#0b5952;--gs-hero-from:#0f766e;--gs-hero-to:#134e4a;
  --gs-up:#d92d20;--gs-down:#079455;
  width:100%;background:var(--gs-bg);color:var(--gs-text);
  font-family:"MiSans","PingFang SC","Microsoft YaHei",sans-serif;
  font-size:19px;line-height:1.65;font-weight:400;padding:0 0 28px;
  display:flex;flex-direction:column;
}
.gs-page.theme-ocean{--gs-bg:#eef3f7;--gs-line:#dde6ee;--gs-accent:#1d6fb8;
  --gs-accent-soft:#e2eef9;--gs-accent-ink:#17589a;--gs-hero-from:#1d6fb8;--gs-hero-to:#0f3f6e;}
.gs-page.theme-forest{--gs-bg:#f0f4ee;--gs-line:#dfe7da;--gs-accent:#2f7d4f;
  --gs-accent-soft:#e3f0e6;--gs-accent-ink:#22603c;--gs-hero-from:#3b8a5a;--gs-hero-to:#1d4a31;}
.gs-page.theme-sunset{--gs-bg:#fbf4ee;--gs-line:#f0e2d6;--gs-accent:#d9480f;
  --gs-accent-soft:#fdeadf;--gs-accent-ink:#ad3a0c;--gs-hero-from:#e8590c;--gs-hero-to:#9c3008;}
.gs-page.theme-sakura{--gs-bg:#fbf2f5;--gs-line:#f1dfe6;--gs-accent:#c2416b;
  --gs-accent-soft:#fae4ec;--gs-accent-ink:#9c2f53;--gs-hero-from:#d6527e;--gs-hero-to:#8a2448;}
.gs-page.theme-violet{--gs-bg:#f3f1fa;--gs-line:#e3def3;--gs-accent:#6d4bd8;
  --gs-accent-soft:#ebe5fb;--gs-accent-ink:#5236b0;--gs-hero-from:#7457e0;--gs-hero-to:#3b2a85;}
.gs-page.theme-slate{--gs-bg:#f2f4f7;--gs-line:#e1e5eb;--gs-accent:#2459d6;
  --gs-accent-soft:#e4ecfb;--gs-accent-ink:#1c46aa;--gs-hero-from:#28354a;--gs-hero-to:#111827;}
.gs-page.theme-ink{--gs-bg:#101418;--gs-surface:#1a2027;--gs-text:#dfe5ec;--gs-title:#f5f7fa;
  --gs-muted:#94a0ad;--gs-line:#2a323c;--gs-accent:#f0b429;--gs-accent-soft:#33291a;
  --gs-accent-ink:#f7cd6b;--gs-hero-from:#2b3440;--gs-hero-to:#0b0e12;
  --gs-up:#f87171;--gs-down:#34d399;}

.gs-hero{position:relative;width:100%;height:320px;overflow:hidden;border-radius:0 0 28px 28px;
  background:var(--gs-hero-to);}
.gs-hero.short{height:240px;}
.gs-hero img,.gs-cover img,.gs-thumb img,.gs-avatar img{position:absolute;left:0;top:0;
  width:100%;height:100%;max-width:none;object-fit:cover;}
.gs-hero-mask{position:absolute;left:0;top:0;right:0;bottom:0;
  background:linear-gradient(180deg,rgba(0,0,0,0.02) 0%,rgba(0,0,0,0.14) 42%,rgba(0,0,0,0.76) 100%);}
.gs-hero-body{position:absolute;left:28px;right:28px;bottom:32px;display:flex;flex-direction:column;gap:10px;}
.gs-hero.plain{height:auto;padding:60px 28px 38px;
  background:linear-gradient(140deg,var(--gs-hero-from) 0%,var(--gs-hero-to) 100%);}
.gs-hero.plain .gs-hero-body{position:relative;left:0;right:0;bottom:0;}
.gs-eyebrow{font-size:16px;font-weight:520;letter-spacing:0.08em;color:rgba(255,255,255,0.88);}
.gs-title{font-size:40px;font-weight:700;line-height:1.22;color:#ffffff;}
.gs-subtitle{font-size:18px;line-height:1.55;color:rgba(255,255,255,0.92);}
.gs-hero .gs-chips{margin-top:6px;}
.gs-hero .gs-chip{background:rgba(255,255,255,0.2);color:#ffffff;}
.gs-hero.tall{height:520px;}

.gs-band,.gs-stat-hero{display:flex;flex-direction:column;gap:10px;padding:44px 28px 30px;
  border-radius:0 0 28px 28px;background:linear-gradient(140deg,var(--gs-hero-from) 0%,var(--gs-hero-to) 100%);}
.gs-band-row{display:flex;align-items:center;gap:16px;}
.gs-band .gs-chip,.gs-stat-hero .gs-chip{background:rgba(255,255,255,0.2);color:#ffffff;}
.gs-band .gs-chips,.gs-stat-hero .gs-chips{margin-top:6px;}
.gs-stat-row{display:flex;align-items:flex-end;flex-wrap:wrap;gap:18px;}
.gs-stat-val{font-size:64px;font-weight:700;line-height:1.05;color:#ffffff;}
.gs-stat-delta{font-size:26px;font-weight:630;line-height:1.3;padding-bottom:6px;color:#ffffff;}
.gs-stat-hero .gs-up{color:#ff8a80;}
.gs-stat-hero .gs-down{color:#6ee7b7;}

.gs-split{display:flex;align-items:stretch;gap:20px;padding:30px 24px 6px;}
.gs-split-body{flex:1;min-width:0;display:flex;flex-direction:column;justify-content:center;gap:10px;}
.gs-split-media{position:relative;width:300px;height:300px;flex:none;border-radius:24px;overflow:hidden;
  background:var(--gs-accent-soft);}
.gs-split-media img{position:absolute;left:0;top:0;width:100%;height:100%;max-width:none;object-fit:cover;}
.gs-split .gs-eyebrow{color:var(--gs-accent);}
.gs-split .gs-title{color:var(--gs-title);}
.gs-split .gs-subtitle{color:var(--gs-muted);}

.gs-section{display:flex;flex-direction:column;gap:12px;padding:0 24px;margin-top:22px;}
.gs-section.tight{margin-top:20px;}
.gs-h2{display:flex;align-items:center;gap:12px;font-size:27px;font-weight:630;
  line-height:1.3;color:var(--gs-title);}
.gs-h2-no{display:flex;align-items:center;justify-content:center;width:42px;height:42px;flex:none;
  border-radius:13px;background:var(--gs-accent);color:#ffffff;font-size:18px;font-weight:700;}
.gs-lead{font-size:17px;line-height:1.6;color:var(--gs-muted);}

.gs-card{display:flex;flex-direction:column;background:var(--gs-surface);color:var(--gs-text);
  border-radius:20px;overflow:hidden;border:1px solid var(--gs-line);
  box-shadow:0 6px 20px rgba(20,24,31,0.06);}
.gs-card-body{display:flex;flex-direction:column;gap:10px;padding:18px 20px 20px;}
.gs-card-title{font-size:23px;font-weight:630;line-height:1.35;color:var(--gs-title);}
.gs-text{font-size:18px;line-height:1.65;color:var(--gs-text);}
.gs-meta{font-size:15px;line-height:1.5;color:var(--gs-muted);}

.gs-cover{position:relative;width:100%;height:200px;overflow:hidden;border-radius:2px;
  background:var(--gs-accent-soft);}
.gs-cover.sm{height:150px;}
.gs-cover.lg{height:260px;}
.gs-on-img{position:absolute;left:14px;top:14px;display:flex;gap:8px;}

.gs-media{display:flex;gap:16px;align-items:flex-start;padding:16px;}
.gs-thumb{position:relative;width:136px;height:136px;flex:none;border-radius:14px;overflow:hidden;
  background:var(--gs-accent-soft);}
.gs-thumb.wide{width:180px;height:124px;}
.gs-media-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px;}
.gs-avatar{position:relative;width:64px;height:64px;flex:none;border-radius:32px;overflow:hidden;
  background:var(--gs-accent-soft);}
.gs-avatar.lg{width:96px;height:96px;border-radius:48px;}
.gs-thumb.sm{width:64px;height:64px;border-radius:12px;}

.gs-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.gs-grid.cols-3{grid-template-columns:1fr 1fr 1fr;gap:10px;}
.gs-grid.cols-3 .gs-cover{height:120px;}
.gs-grid.cols-3 .gs-tile-title{font-size:17px;}
.gs-tile{display:flex;flex-direction:column;min-width:0;background:var(--gs-surface);border-radius:16px;
  overflow:hidden;border:1px solid var(--gs-line);}
.gs-tile .gs-cover{height:140px;}
.gs-tile-body{display:flex;flex-direction:column;gap:4px;padding:12px 14px 14px;}
.gs-tile-title{font-size:19px;font-weight:630;line-height:1.35;color:var(--gs-title);}

.gs-chips{display:flex;flex-wrap:wrap;gap:8px;}
.gs-chip{display:flex;align-items:center;justify-content:center;padding:5px 12px;border-radius:999px;
  background:var(--gs-accent-soft);color:var(--gs-accent-ink);font-size:15px;font-weight:520;
  line-height:1.2;white-space:nowrap;word-break:keep-all;flex-shrink:0;}
.gs-chip.solid{background:var(--gs-accent);color:#ffffff;}
.gs-chip.ghost{background:var(--gs-surface);border:1px solid var(--gs-line);color:var(--gs-muted);}

.gs-kpis{display:flex;gap:12px;}
.gs-kpi{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px;padding:16px;border-radius:18px;
  background:var(--gs-surface);border:1px solid var(--gs-line);}
.gs-kpi-val{font-size:30px;font-weight:700;line-height:1.15;color:var(--gs-title);}
.gs-kpi-lab{font-size:15px;line-height:1.4;color:var(--gs-muted);}

.gs-timeline{display:flex;flex-direction:column;}
.gs-tl{display:flex;gap:14px;}
.gs-tl-axis{width:18px;flex:none;display:flex;flex-direction:column;align-items:center;}
.gs-tl-dot{width:14px;height:14px;flex:none;border-radius:7px;margin-top:7px;background:var(--gs-accent);}
.gs-tl-line{width:2px;flex:1;margin-top:6px;background:var(--gs-line);}
.gs-tl-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:8px;padding-bottom:22px;}
.gs-tl-time{font-size:15px;font-weight:630;letter-spacing:0.04em;color:var(--gs-accent);}

.gs-list{display:flex;flex-direction:column;gap:10px;}
.gs-li{display:flex;gap:12px;align-items:flex-start;font-size:18px;line-height:1.6;}
.gs-li-dot{width:8px;height:8px;flex:none;border-radius:4px;margin-top:12px;background:var(--gs-accent);}
.gs-kv{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;padding:10px 0;
  border-bottom:1px solid var(--gs-line);font-size:17px;line-height:1.5;}
.gs-k{flex:none;color:var(--gs-muted);}
.gs-v{flex:1;min-width:0;text-align:right;font-weight:520;color:var(--gs-text);}

.gs-tip{display:flex;gap:12px;align-items:flex-start;padding:16px 18px;border-radius:16px;
  background:var(--gs-accent-soft);color:var(--gs-accent-ink);font-size:17px;line-height:1.6;}
.gs-ico{width:26px;height:26px;flex:none;}
.gs-ico.lg{width:34px;height:34px;}
.gs-ico img{width:100%;height:100%;}
.gs-tip .gs-ico{margin-top:1px;}

.gs-rank{display:flex;flex-direction:column;background:var(--gs-surface);border-radius:20px;
  border:1px solid var(--gs-line);overflow:hidden;}
.gs-rank-row{display:flex;align-items:center;gap:14px;padding:14px 18px;border-bottom:1px solid var(--gs-line);}
.gs-rank-row.top{background:var(--gs-accent-soft);}
.gs-rank-no{display:flex;align-items:center;justify-content:center;width:40px;height:40px;flex:none;
  border-radius:12px;background:var(--gs-bg);color:var(--gs-muted);font-size:19px;font-weight:700;}
.gs-rank-row.top .gs-rank-no{background:var(--gs-accent);color:#ffffff;}
.gs-rank-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px;}
.gs-rank-name{font-size:19px;font-weight:630;line-height:1.35;color:var(--gs-title);}
.gs-rank-val{flex:none;font-size:22px;font-weight:700;line-height:1.2;color:var(--gs-title);text-align:right;}

.gs-compare-head{display:flex;align-items:center;gap:12px;margin-top:8px;}
.gs-compare-side{flex:1;min-width:0;display:flex;flex-direction:column;align-items:center;gap:8px;
  padding:16px 12px;border-radius:20px;background:rgba(255,255,255,0.14);}
.gs-compare-name{font-size:22px;font-weight:700;line-height:1.3;color:#ffffff;text-align:center;}
.gs-compare-side .gs-meta{color:rgba(255,255,255,0.8);text-align:center;}
.gs-compare-vs{flex:none;font-size:24px;font-weight:700;color:#ffffff;}
.gs-compare-table{display:flex;flex-direction:column;background:var(--gs-surface);border-radius:20px;
  border:1px solid var(--gs-line);overflow:hidden;}
.gs-compare-row{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--gs-line);
  font-size:18px;line-height:1.45;}
.gs-compare-a,.gs-compare-b{flex:1;min-width:0;color:var(--gs-text);}
.gs-compare-a{text-align:right;}
.gs-compare-b{text-align:left;}
.gs-compare-k{width:140px;flex:none;text-align:center;font-size:15px;color:var(--gs-muted);}
.gs-compare-a.win,.gs-compare-b.win{font-weight:700;color:var(--gs-accent);}

.gs-quote{display:flex;flex-direction:column;gap:10px;padding:22px 24px;border-radius:20px;
  background:var(--gs-accent-soft);}
.gs-quote-text{font-size:24px;font-weight:630;line-height:1.55;color:var(--gs-accent-ink);}
.gs-quote-by{font-size:15px;line-height:1.5;color:var(--gs-muted);text-align:right;}

.gs-steps{display:flex;flex-direction:column;gap:12px;}
.gs-step{display:flex;gap:14px;align-items:flex-start;padding:16px 18px;border-radius:18px;
  background:var(--gs-surface);border:1px solid var(--gs-line);}
.gs-step-no{display:flex;align-items:center;justify-content:center;width:36px;height:36px;flex:none;
  border-radius:18px;background:var(--gs-accent);color:#ffffff;font-size:17px;font-weight:700;}
.gs-step-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px;}

.gs-row{display:flex;gap:12px;align-items:center;}
.gs-row.top{align-items:flex-start;}
.gs-col{flex:1;min-width:0;display:flex;flex-direction:column;gap:6px;}
.gs-accent{color:var(--gs-accent);}
.gs-b{font-weight:630;color:var(--gs-title);}
.gs-up{color:var(--gs-up);}
.gs-down{color:var(--gs-down);}
.gs-footer{margin:40px 24px 0;padding-top:16px;border-top:1px solid var(--gs-line);
  font-size:15px;line-height:1.6;color:var(--gs-muted);}
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
