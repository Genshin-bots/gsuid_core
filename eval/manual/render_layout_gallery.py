"""离线渲染 render_agent 的 8 种版式样例，肉眼检查 Takumi 下组件是否塌版。

用法：``uv run python eval/manual/render_layout_gallery.py``，输出到 ``test_output/layouts/``。
照片用本地生成的色块图，不联网。
"""

from __future__ import annotations

import sys
import asyncio
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "test_output" / "layouts"


def _photo(width: int, height: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> bytes:
    im = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(im)
    for y in range(height):
        k = y / max(1, height - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * k) for i in range(3))
        draw.line([(0, y), (width, y)], fill=color)
    draw.ellipse([width * 0.6, height * 0.15, width * 0.85, height * 0.15 + width * 0.25], fill=(255, 236, 179))
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


def _cases(img: list[str], bar_svg: str, signed_svg: str) -> dict[str, str]:
    footer = '<div class="gs-footer">来源：样例数据 · 2026-10-23</div>'
    chips = '<div class="gs-chips"><div class="gs-chip">标签一</div><div class="gs-chip">标签二</div></div>'
    tile = (
        '<div class="gs-tile"><div class="gs-cover"><img src="{src}"></div>'
        '<div class="gs-tile-body"><div class="gs-tile-title">{name}</div>'
        '<div class="gs-meta">{meta}</div></div></div>'
    )
    return {
        "feature": f"""
<div class="gs-page layout-feature theme-ocean">
  <div class="gs-hero"><img src="{img[0]}"><div class="gs-hero-mask"></div>
    <div class="gs-hero-body"><div class="gs-eyebrow">3 天 2 晚 · 自驾</div>
      <div class="gs-title">三天走完山海线</div>
      <div class="gs-subtitle">两座山、一片海、四家老店</div>{chips}</div></div>
  <div class="gs-section"><div class="gs-h2"><div class="gs-h2-no">1</div>第一天 · 山</div>
    <div class="gs-card"><div class="gs-cover"><img src="{img[1]}">
      <div class="gs-on-img"><div class="gs-chip solid">必去</div></div></div>
      <div class="gs-card-body"><div class="gs-card-title">云海步道</div>
        <div class="gs-text">清晨六点前上山，云海概率最高。</div>
        <div class="gs-chips"><div class="gs-chip">门票 ¥120</div><div class="gs-chip">4 小时</div></div></div></div>
    <div class="gs-card"><div class="gs-media"><div class="gs-thumb"><img src="{img[2]}"></div>
      <div class="gs-media-body"><div class="gs-card-title">山脚面馆</div>
        <div class="gs-text">本地人排队的一家，汤底清。</div>
        <div class="gs-meta">人均 ¥35 · 7:00 开门</div></div></div></div>
  </div>
  {footer}
</div>""",
        "gallery": f"""
<div class="gs-page layout-gallery theme-sakura">
  <div class="gs-split"><div class="gs-split-body"><div class="gs-eyebrow">图鉴 · 第 3 期</div>
    <div class="gs-title">新版本六位角色</div><div class="gs-subtitle">按定位分组，一眼看全</div></div>
    <div class="gs-split-media"><img src="{img[0]}"></div></div>
  <div class="gs-section"><div class="gs-h2">输出位</div>
    <div class="gs-grid cols-3">
      {tile.format(src=img[1], name="角色甲", meta="火 · 单体")}
      {tile.format(src=img[2], name="角色乙", meta="冰 · 群体")}
      {tile.format(src=img[3], name="角色丙", meta="雷 · 持续")}
      {tile.format(src=img[4], name="角色丁", meta="风 · 聚怪")}
      {tile.format(src=img[1], name="角色戊", meta="水 · 治疗")}
      {tile.format(src=img[2], name="角色己", meta="岩 · 护盾")}
    </div></div>
  {footer}
</div>""",
        "magazine": f"""
<div class="gs-page layout-magazine theme-violet">
  <div class="gs-hero tall"><img src="{img[0]}"><div class="gs-hero-mask"></div>
    <div class="gs-hero-body"><div class="gs-eyebrow">人物 · 深度</div>
      <div class="gs-title">一位老匠人的四十年</div><div class="gs-subtitle">从学徒到非遗传承人</div></div></div>
  <div class="gs-section"><div class="gs-quote"><div class="gs-quote-text">手上慢一点，东西才能用得久一点。</div>
    <div class="gs-quote-by">—— 采访原话</div></div></div>
  <div class="gs-section"><div class="gs-row top">
    <div class="gs-col"><div class="gs-kv"><div class="gs-k">出生</div><div class="gs-v">1962</div></div>
      <div class="gs-kv"><div class="gs-k">入行</div><div class="gs-v">1984</div></div>
      <div class="gs-kv"><div class="gs-k">作品</div><div class="gs-v">300+ 件</div></div></div>
    <div class="gs-col"><div class="gs-card"><div class="gs-cover sm"><img src="{img[3]}"></div>
      <div class="gs-card-body"><div class="gs-meta">工作间一角</div></div></div></div>
  </div></div>
  {footer}
</div>""",
        "dashboard": f"""
<div class="gs-page layout-dashboard theme-slate">
  <div class="gs-stat-hero"><div class="gs-eyebrow">样例指数 · 日 K · 收盘</div>
    <div class="gs-title">样例指数</div>
    <div class="gs-stat-row"><div class="gs-stat-val">1,245.36</div>
      <div class="gs-stat-delta gs-up">+2.31%</div></div>{chips}</div>
  <div class="gs-section"><div class="gs-kpis">
    <div class="gs-kpi"><div class="gs-kpi-val gs-up">+28.1</div><div class="gs-kpi-lab">涨跌点</div></div>
    <div class="gs-kpi"><div class="gs-kpi-val">812 亿</div><div class="gs-kpi-lab">成交额</div></div>
    <div class="gs-kpi"><div class="gs-kpi-val gs-down">-0.4%</div><div class="gs-kpi-lab">近 5 日</div></div></div>
    <div class="gs-card"><div class="gs-card-body"><div class="gs-card-title">近 6 日涨跌幅</div>{signed_svg}
      <div class="gs-meta">正值红、负值绿，与上方数字同色</div></div></div>
    <div class="gs-card"><div class="gs-card-body">
      <div class="gs-kv"><div class="gs-k">开盘</div><div class="gs-v">1,221.08</div></div>
      <div class="gs-kv"><div class="gs-k">最高</div><div class="gs-v">1,250.12</div></div>
      <div class="gs-kv"><div class="gs-k">最低</div><div class="gs-v">1,218.40</div></div></div></div>
    <div class="gs-tip">波动加大，样例数据不构成任何建议。</div>
  </div>
  {footer}
</div>""",
        "ranking": f"""
<div class="gs-page layout-ranking theme-sunset">
  <div class="gs-band"><div class="gs-eyebrow">本周榜单</div><div class="gs-title">人气店铺 Top 5</div>
    <div class="gs-subtitle">按评价数排序</div></div>
  <div class="gs-section"><div class="gs-rank">
    <div class="gs-rank-row top"><div class="gs-rank-no">1</div><div class="gs-thumb sm"><img src="{img[1]}"></div>
      <div class="gs-rank-body"><div class="gs-rank-name">老街烧饼</div><div class="gs-meta">人均 ¥12</div></div>
      <div class="gs-rank-val">4,812</div></div>
    <div class="gs-rank-row top"><div class="gs-rank-no">2</div><div class="gs-thumb sm"><img src="{img[2]}"></div>
      <div class="gs-rank-body"><div class="gs-rank-name">海边排档</div><div class="gs-meta">人均 ¥88</div></div>
      <div class="gs-rank-val">3,977</div></div>
    <div class="gs-rank-row top"><div class="gs-rank-no">3</div><div class="gs-thumb sm"><img src="{img[3]}"></div>
      <div class="gs-rank-body"><div class="gs-rank-name">巷口甜品</div><div class="gs-meta">人均 ¥25</div></div>
      <div class="gs-rank-val">3,301</div></div>
    <div class="gs-rank-row"><div class="gs-rank-no">4</div>
      <div class="gs-rank-body"><div class="gs-rank-name">深夜面馆</div><div class="gs-meta">人均 ¥30</div></div>
      <div class="gs-rank-val">2,640</div></div>
    <div class="gs-rank-row"><div class="gs-rank-no">5</div>
      <div class="gs-rank-body"><div class="gs-rank-name">早茶小馆</div><div class="gs-meta">人均 ¥60</div></div>
      <div class="gs-rank-val">2,118</div></div>
  </div></div>
  {footer}
</div>""",
        "compare": f"""
<div class="gs-page layout-compare theme-ink">
  <div class="gs-band"><div class="gs-eyebrow">对比</div><div class="gs-title">两款机型怎么选</div>
    <div class="gs-compare-head">
      <div class="gs-compare-side"><div class="gs-avatar lg"><img src="{img[1]}"></div>
        <div class="gs-compare-name">机型 A</div><div class="gs-meta">¥3,999</div></div>
      <div class="gs-compare-vs">VS</div>
      <div class="gs-compare-side"><div class="gs-avatar lg"><img src="{img[2]}"></div>
        <div class="gs-compare-name">机型 B</div><div class="gs-meta">¥4,299</div></div></div></div>
  <div class="gs-section"><div class="gs-compare-table">
    <div class="gs-compare-row"><div class="gs-compare-a win">5500 mAh</div><div class="gs-compare-k">电池</div>
      <div class="gs-compare-b">5000 mAh</div></div>
    <div class="gs-compare-row"><div class="gs-compare-a">198 g</div><div class="gs-compare-k">重量</div>
      <div class="gs-compare-b win">186 g</div></div>
    <div class="gs-compare-row"><div class="gs-compare-a">50 MP</div><div class="gs-compare-k">主摄</div>
      <div class="gs-compare-b win">64 MP</div></div></div>
    <div class="gs-card"><div class="gs-card-body"><div class="gs-card-title">跑分对比</div>{bar_svg}</div></div>
    <div class="gs-tip">重续航选 A，重轻薄和拍照选 B。</div></div>
  {footer}
</div>""",
        "timeline": f"""
<div class="gs-page layout-timeline theme-forest">
  <div class="gs-band"><div class="gs-eyebrow">版本历程</div><div class="gs-title">一年四次大更新</div>
    <div class="gs-subtitle">每次更新的重点</div></div>
  <div class="gs-section"><div class="gs-timeline">
    <div class="gs-tl"><div class="gs-tl-axis"><div class="gs-tl-dot"></div><div class="gs-tl-line"></div></div>
      <div class="gs-tl-body"><div class="gs-tl-time">2026-01</div><div class="gs-card"><div class="gs-card-body">
        <div class="gs-card-title">1.0 上线</div><div class="gs-text">基础功能与首批内容。</div></div></div></div></div>
    <div class="gs-tl"><div class="gs-tl-axis"><div class="gs-tl-dot"></div><div class="gs-tl-line"></div></div>
      <div class="gs-tl-body"><div class="gs-tl-time">2026-04</div><div class="gs-card"><div class="gs-card-body">
        <div class="gs-card-title">1.1 新地图</div><div class="gs-text">开放第二张地图。</div></div></div></div></div>
    <div class="gs-tl"><div class="gs-tl-axis"><div class="gs-tl-dot"></div></div>
      <div class="gs-tl-body"><div class="gs-tl-time">2026-07</div><div class="gs-card"><div class="gs-card-body">
        <div class="gs-card-title">1.2 联机</div><div class="gs-text">加入四人联机。</div></div></div></div></div>
  </div></div>
  {footer}
</div>""",
        "brief": f"""
<div class="gs-page layout-brief theme-paper">
  <div class="gs-band"><div class="gs-eyebrow">教程</div><div class="gs-title">三步装好开发环境</div>
    <div class="gs-subtitle">Windows 与 macOS 通用</div></div>
  <div class="gs-section"><div class="gs-steps">
    <div class="gs-step"><div class="gs-step-no">1</div>
      <div class="gs-step-body"><div class="gs-card-title">装 Python</div>
      <div class="gs-text">用 uv 装 3.12，不要用系统自带的。</div></div></div>
    <div class="gs-step"><div class="gs-step-no">2</div>
      <div class="gs-step-body"><div class="gs-card-title">同步依赖</div>
      <div class="gs-text">在仓库根目录执行 uv sync。</div></div></div>
    <div class="gs-step"><div class="gs-step-no">3</div>
      <div class="gs-step-body"><div class="gs-card-title">启动</div>
      <div class="gs-text">执行 uv run core，浏览器打开 8765 端口。</div></div></div></div>
    <div class="gs-list"><div class="gs-li"><div class="gs-li-dot"></div>首次启动会生成 data 目录。</div>
      <div class="gs-li"><div class="gs-li-dot"></div>公网部署必须配 WS_TOKEN。</div></div>
    <div class="gs-tip">端口被占用时用 --port 换一个。</div></div>
  {footer}
</div>""",
    }


async def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    from gsuid_core.utils.html_render import render_html_to_bytes
    from gsuid_core.utils.resource_manager import RM
    from gsuid_core.ai_core.buildin_tools.chart_svg import chart_spec_to_svg
    from gsuid_core.ai_core.buildin_tools.render_kit import RENDER_LAYOUTS
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _prepare_free_html, _auto_embed_html_images

    palettes = [
        ((56, 120, 190), (18, 52, 96)),
        ((120, 180, 120), (40, 90, 60)),
        ((230, 150, 90), (150, 60, 30)),
        ((170, 130, 210), (70, 40, 120)),
        ((90, 170, 190), (30, 80, 100)),
    ]
    img = [RM.register(_photo(1600, 1000, top, bottom)) for top, bottom in palettes]
    signed_svg = chart_spec_to_svg(
        {
            "type": "bar",
            "signed": True,
            "dark": False,
            "width": 520,
            "height": 280,
            "data": [{"label": f"10-{d}", "value": v} for d, v in zip(range(18, 24), [1.2, -0.6, 0.8, -1.4, 0.3, 2.3])],
        }
    )
    bar_svg = chart_spec_to_svg(
        {
            "type": "bar",
            "dark": True,
            "width": 520,
            "height": 260,
            "series": [
                {"name": "机型 A", "data": [{"label": "CPU", "value": 182}, {"label": "GPU", "value": 140}]},
                {"name": "机型 B", "data": [{"label": "CPU", "value": 171}, {"label": "GPU", "value": 156}]},
            ],
        }
    )
    cases = _cases(img, bar_svg, signed_svg)
    assert set(cases) == set(RENDER_LAYOUTS)
    for name, body in cases.items():
        html = _prepare_free_html(await _auto_embed_html_images(body))
        data = await render_html_to_bytes(
            html,
            max_width=840.0 * 2,
            dpi=192.0,
            image_format="png",
            default_font_size=16.0,
            root_max_width=840.0,
        )
        out = OUTPUT_DIR / f"{name}.png"
        out.write_bytes(data)
        with Image.open(BytesIO(data)) as im:
            print(f"{name}: {im.size[0]}x{im.size[1]} ratio={im.size[1] / im.size[0]:.2f} -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
