"""离线渲染质量测试：验证 HTML/Markdown 渲染产出可视化图片。

无需启动服务，直接调用渲染引擎。
用法::
    .venv\\Scripts\\python.exe test_render_quality.py
"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

OUTPUT_DIR = Path(__file__).resolve().parent / "test_output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


WEATHER_HTML = """
<h1>广州 · 七日天气</h1>
<div class="meta">数据时点：2026-07-29 08:00 · 来源：中国气象局</div>
<div class="grid">
  <div class="metric"><div class="lab">今日温度</div><div class="val">33°</div>
    <div class="sub">雷阵雨转多云</div></div>
  <div class="metric"><div class="lab">湿度</div><div class="val">82%</div>
    <div class="sub">闷热体感</div></div>
  <div class="metric"><div class="lab">紫外线</div><div class="val">强</div>
    <div class="sub">注意防晒</div></div>
</div>
<h2>逐日预报</h2>
<div class="daygrid">
  <div class="day"><div class="d">周二</div><div class="ico storm">雷</div>
    <div class="w">雷阵雨</div>
    <div class="t"><span class="hi">33</span>/<span class="lo">26</span></div></div>
  <div class="day"><div class="d">周三</div><div class="ico rain">雨</div>
    <div class="w">中到大雨</div>
    <div class="t"><span class="hi">31</span>/<span class="lo">25</span></div></div>
  <div class="day"><div class="d">周四</div><div class="ico rain">雨</div>
    <div class="w">阵雨</div>
    <div class="t"><span class="hi">32</span>/<span class="lo">26</span></div></div>
  <div class="day"><div class="d">周五</div><div class="ico cloud">云</div>
    <div class="w">多云</div>
    <div class="t"><span class="hi">34</span>/<span class="lo">27</span></div></div>
  <div class="day"><div class="d">周六</div><div class="ico sun">晴</div>
    <div class="w">晴间多云</div>
    <div class="t"><span class="hi">35</span>/<span class="lo">28</span></div></div>
  <div class="day"><div class="d">周日</div><div class="ico hot">热</div>
    <div class="w">晴热</div>
    <div class="t"><span class="hi">36</span>/<span class="lo">28</span></div></div>
  <div class="day"><div class="d">周一</div><div class="ico storm">雷</div>
    <div class="w">雷阵雨</div>
    <div class="t"><span class="hi">33</span>/<span class="lo">26</span></div></div>
</div>
<h2>生活建议</h2>
<div class="row"><div class="ico sm warn">防</div>
  <div class="main"><div class="title">防晒防暑</div>
  <div class="desc">紫外线强，外出做好防晒，多补水</div></div></div>
<div class="row"><div class="ico sm rain">雨</div>
  <div class="main"><div class="title">携带雨具</div>
  <div class="desc">本周多雷阵雨，出门带伞</div></div></div>
"""

NEWS_HTML = """
<h1>晨间新闻速览</h1>
<div class="meta">2026-07-29 周二 · 综合要闻</div>
<div class="row"><div class="ico sm news">国</div>
  <div class="main"><div class="title">国务院常务会议部署稳增长举措</div>
  <div class="desc">强调加大宏观政策调控力度，着力扩大内需</div></div>
  <div class="tag">时政</div></div>
<div class="row"><div class="ico sm ok">科</div>
  <div class="main"><div class="title">国产大模型通过图灵测试</div>
  <div class="desc">多家机构联合评测，首次达到人类水平对话能力</div></div>
  <div class="tag green">科技</div></div>
<div class="row"><div class="ico sm warn">财</div>
  <div class="main"><div class="title">A股三大指数集体收涨</div>
  <div class="desc">沪指涨1.2%重回3200点，成交额破万亿</div></div>
  <div class="tag gold">财经</div></div>
<div class="row"><div class="ico sm cool">体</div>
  <div class="main"><div class="title">中国女排3:1击败巴西</div>
  <div class="desc">世界联赛总决赛半决赛，挺进决赛</div></div>
  <div class="tag">体育</div></div>
<div class="row"><div class="ico sm bad">天</div>
  <div class="main"><div class="title">台风"杜苏芮"残余环流影响华南</div>
  <div class="desc">广东福建局地暴雨，注意防范地质灾害</div></div>
  <div class="tag red">天气</div></div>
"""

STOCK_HTML = """
<h1>东山精密 · 个股速览</h1>
<div class="meta">数据截至 2026-07-29 收盘 · 仅供参考</div>
<div class="grid">
  <div class="metric"><div class="lab">今收</div><div class="val">28.56</div>
    <div class="sub">+3.21%</div></div>
  <div class="metric"><div class="lab">成交量</div><div class="val">1.2亿</div>
    <div class="sub">放量突破</div></div>
  <div class="metric"><div class="lab">市盈率</div><div class="val">32.4</div>
    <div class="sub">行业中位</div></div>
</div>
<h2>近期走势</h2>
<div class="row"><div class="ico sm ok">涨</div>
  <div class="main"><div class="title">5日均线金叉10日</div>
  <div class="desc">短期趋势转多，MACD红柱放大</div></div>
  <div class="tag green">看多</div></div>
<div class="row"><div class="ico sm warn">量</div>
  <div class="main"><div class="title">成交量连续3日放大</div>
  <div class="desc">资金关注度提升，北向净买入2.3亿</div></div>
  <div class="tag gold">关注</div></div>
<h2>基本面</h2>
<div class="row"><div class="ico sm news">业</div>
  <div class="main"><div class="title">Q2业绩预告超预期</div>
  <div class="desc">净利润同比+45%，FPC业务高增长</div></div></div>
"""


async def main() -> None:
    from gsuid_core.utils.html_render import render_md_to_bytes, render_html_to_bytes

    css_path = str(
        Path(__file__).resolve().parent.parent / "gsuid_core" / "utils" / "html_render" / "markdown_dark.css"
    )

    print("=" * 60)
    print("离线渲染质量测试")
    print("=" * 60)

    # 1. 天气 HTML
    print("\n[1/4] 渲染天气 HTML...")
    # 可选 design shell（显式）；render_html_to_image 默认已不套壳
    from gsuid_core.ai_core.buildin_tools.html_render_tools import _wrap_with_design_shell

    weather_full = _wrap_with_design_shell(WEATHER_HTML)
    img = await render_html_to_bytes(weather_full, max_width=720, image_format="png", default_font_size=15.0)
    out = OUTPUT_DIR / "quality_weather.png"
    out.write_bytes(img)
    print(f"  ✅ {out} ({len(img) // 1024}KB)")

    # 2. 新闻 HTML
    print("\n[2/4] 渲染新闻 HTML...")
    news_full = _wrap_with_design_shell(NEWS_HTML)
    img = await render_html_to_bytes(news_full, max_width=720, image_format="png", default_font_size=15.0)
    out = OUTPUT_DIR / "quality_news.png"
    out.write_bytes(img)
    print(f"  ✅ {out} ({len(img) // 1024}KB)")

    # 3. 股票 HTML
    print("\n[3/4] 渲染股票 HTML...")
    stock_full = _wrap_with_design_shell(STOCK_HTML)
    img = await render_html_to_bytes(stock_full, max_width=720, image_format="png", default_font_size=15.0)
    out = OUTPUT_DIR / "quality_stock.png"
    out.write_bytes(img)
    print(f"  ✅ {out} ({len(img) // 1024}KB)")

    # 4. Markdown 渲染
    print("\n[4/4] 渲染 Markdown...")
    md_content = """# 本周大事记

数据时点：2026-07-29

## 国际

- 联合国气候大会达成新减排协议
- 欧盟通过人工智能监管法案

## 国内

- 高铁新线路开通运营
- 多地出台促消费政策

## 科技

- 量子计算突破：首次实现1000量子比特
- 国产操作系统生态联盟成立
"""
    img = await render_md_to_bytes(
        md=md_content,
        css_path=css_path,
        max_width=720,
        image_format="png",
        dark=False,
    )
    out = OUTPUT_DIR / "quality_markdown.png"
    out.write_bytes(img)
    print(f"  ✅ {out} ({len(img) // 1024}KB)")

    print(f"\n全部完成！图片目录: {OUTPUT_DIR}")
    print("请检查图片是否具有可视化效果（卡片/指标/图标），而非纯文本。")


if __name__ == "__main__":
    asyncio.run(main())
