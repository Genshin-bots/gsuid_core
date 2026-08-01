"""离线验证 render_card 模板与 freehand 设计系统壳。"""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUTPUT_DIR = Path(__file__).resolve().parent / "test_output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


async def main() -> None:
    from gsuid_core.utils.html_render import render_html_to_bytes
    from gsuid_core.ai_core.buildin_tools.html_render_tools import (
        _build_card_html,
        _wrap_html_if_needed,
    )

    cases: list[tuple[str, str]] = []

    weather_html = _build_card_html(
        "weather",
        "广州 · 七日天气",
        "2026-07-29 · 中国气象局",
        {
            "metrics": [
                {"label": "今日温度", "value": "33°", "sub": "雷阵雨转多云"},
                {"label": "湿度", "value": "82%", "sub": "闷热体感"},
                {"label": "紫外线", "value": "强", "sub": "注意防晒"},
            ],
            "days": [
                {"day": "周二", "icon": "雷", "weather": "雷阵雨", "high": "33", "low": "26"},
                {"day": "周三", "icon": "雨", "weather": "中到大雨", "high": "31", "low": "25"},
                {"day": "周四", "icon": "雨", "weather": "阵雨", "high": "32", "low": "26"},
                {"day": "周五", "icon": "云", "weather": "多云", "high": "34", "low": "27"},
                {"day": "周六", "icon": "晴", "weather": "晴间多云", "high": "35", "low": "28"},
                {"day": "周日", "icon": "热", "weather": "晴热", "high": "36", "low": "28"},
                {"day": "周一", "icon": "雷", "weather": "雷阵雨", "high": "33", "low": "26"},
            ],
            "tips": [
                {"icon": "防", "title": "防晒防暑", "desc": "紫外线强，外出做好防晒"},
                {"icon": "雨", "title": "携带雨具", "desc": "本周多雷阵雨，出门带伞"},
            ],
        },
    )
    cases.append(("card_weather.png", weather_html))

    news_html = _build_card_html(
        "news",
        "晨间新闻速览",
        "2026-07-29 周二 · 综合要闻",
        {
            "items": [
                {
                    "icon": "国",
                    "title": "国务院常务会议部署稳增长举措",
                    "desc": "加大宏观政策调控力度，着力扩大内需",
                    "tag": "时政",
                },
                {
                    "icon": "晴",
                    "title": "国产大模型评测新高",
                    "desc": "多家机构联合评测，对话能力显著提升",
                    "tag": "科技",
                },
                {
                    "icon": "热",
                    "title": "A股三大指数集体收涨",
                    "desc": "沪指涨1.2%重回3200点，成交额破万亿",
                    "tag": "财经",
                },
                {
                    "icon": "云",
                    "title": "中国女排3:1击败巴西",
                    "desc": "世界联赛总决赛半决赛，挺进决赛",
                    "tag": "体育",
                },
            ]
        },
    )
    cases.append(("card_news.png", news_html))

    board_html = _build_card_html(
        "board",
        "模拟盘持仓速览",
        "数据时点 2026-07-29 · 仅供参考",
        {
            "metrics": [
                {"label": "总资产", "value": "100.06万", "sub": "+0.06%"},
                {"label": "现金", "value": "89.96万", "sub": ""},
                {"label": "持仓市值", "value": "10.10万", "sub": "+0.63%"},
            ],
            "sections": [
                {
                    "title": "当前持仓",
                    "rows": [
                        {"left": "东山精密", "mid": "300股 @27.5", "right": "+3.2%"},
                        {"left": "中信证券", "mid": "200股 @22.1", "right": "-0.8%"},
                    ],
                }
            ],
        },
    )
    cases.append(("card_board.png", board_html))

    freehand = _wrap_html_if_needed(
        """
<h1>广州 · 七日天气</h1>
<div class="meta">数据时点：2026-07-29</div>
<div class="grid">
  <div class="metric"><div class="lab">今日</div><div class="val">33°</div>
    <div class="sub">雷阵雨</div></div>
  <div class="metric"><div class="lab">湿度</div><div class="val">82%</div>
    <div class="sub">闷热</div></div>
  <div class="metric"><div class="lab">紫外线</div><div class="val">强</div>
    <div class="sub">注意防晒</div></div>
</div>
<h2>逐日预报</h2>
<div class="daygrid">
  <div class="day"><div class="d">周二</div><div class="ico storm">雷</div>
    <div class="w">雷阵雨</div>
    <div class="t"><span class="hi">33</span><span class="lo">/26</span></div></div>
  <div class="day"><div class="d">周三</div><div class="ico rain">雨</div>
    <div class="w">大雨</div>
    <div class="t"><span class="hi">31</span><span class="lo">/25</span></div></div>
  <div class="day"><div class="d">周四</div><div class="ico cloud">云</div>
    <div class="w">多云</div>
    <div class="t"><span class="hi">34</span><span class="lo">/27</span></div></div>
</div>
<div class="row"><div class="ico sm warn">防</div>
  <div class="main"><div class="title">防晒防暑</div>
  <div class="desc">紫外线强，外出做好防晒</div></div></div>
"""
    )
    cases.append(("card_freehand.png", freehand))

    for name, html in cases:
        img = await render_html_to_bytes(
            html,
            max_width=1440.0,
            dpi=192.0,
            default_font_size=15.0,
            image_format="png",
        )
        path = OUTPUT_DIR / name
        path.write_bytes(img)
        print(f"OK {path.name} ({len(img) // 1024}KB)")

    print(f"\n输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
