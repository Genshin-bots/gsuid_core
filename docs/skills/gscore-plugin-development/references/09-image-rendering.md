# 九、图片渲染范式

GsCore 中**首选 PIL 直接绘图**——可控、轻量、无外部依赖、跨平台稳定。仅当 PIL 表达能力
不够（复杂表格 / 富文本 / 图表）时才升级到 HTML 渲染。

## 9.1 三档渲染方案（优先级从高到低）

| 档位 | 库 | 适用场景 | 主要缺点 |
|------|----|---------|---------|
| **① PIL（首选）** | `Pillow` + `gsuid_core.utils.image.image_tools` | 角色面板 / 卡片 / 排行榜 / 半结构化展示 | 排版手写、长文本麻烦 |
| **② htmlkit（推荐）** | `gsuid_core.utils.html_render.render_html_to_bytes / render_md_to_bytes` | Markdown 报告 / 表格 / 简单 HTML | 不能跑 JS、不渲染 SVG 动画 |
| **③ playwright（兜底）** | `playwright.async_api.async_playwright` | 需要 JS / Plotly / ECharts / 图表交互的复杂可视化 | 启动重、依赖 chromium、首次需 `playwright install` |

> **决策口诀**：
> - 能用 PIL 拼出来的，就不要走 HTML。
> - 能用 htmlkit 渲染纯 HTML / Markdown 的，就不要拉 playwright。
> - 只有"非 JS 引擎渲染不出来"的图（K线、云图、3D Plotly）才上 playwright，并显式声明
>   `playwright>=1.49.0` 依赖、写明用户需手动 `playwright install`。

## 9.2 PIL 范式（首选）

利用 `gsuid_core.utils.image.image_tools` 提供的**复用度极高**的工具函数：

```python
from PIL import Image, ImageDraw

from gsuid_core.utils.fonts.fonts import core_font
from gsuid_core.utils.image.image_tools import (
    get_color_bg,        # 自动从图库 + 主色 mask 生成背景
    crop_center_img,     # 居中裁切到指定尺寸
    easy_paste,          # 按 lt/lm/rb/center 等方向贴图
    easy_alpha_composite,
    draw_pic_with_ring,  # 头像加圆环（异步）
    CustomizeImage,      # 从自定义背景目录随机取图 + 提取主色
)
from gsuid_core.utils.image.convert import convert_img


async def render_role_card(uid: str, name: str, data: dict) -> bytes:
    # 1. 背景（自动主色 mask；bg_path 可指向插件的 CU_BG_PATH）
    img = await get_color_bg(based_w=950, based_h=1400)

    # 2. 文字
    draw = ImageDraw.Draw(img)
    draw.text((48, 60), f"角色: {name}", font=core_font(48), fill="white")
    draw.text((48, 120), f"UID: {uid}", font=core_font(32), fill=(200, 200, 200))

    # 3. 头像加圆环
    avatar = Image.open("xxx.png")
    ring_avatar = await draw_pic_with_ring(avatar, 200)
    easy_paste(img, ring_avatar, (380, 200), direction="center")

    # 4. 转字节并返回（convert_img 会按当前框架配置做缩放 / base64 等处理）
    return await convert_img(img)
```

**用 `core_font(size)` 拿字体**（自动选用框架预置的中英文兜底字体），不要 hardcode 字体路径。

## 9.3 htmlkit 范式（推荐）

适合一次性、不需要交互的 Markdown 报告 / 简单 HTML 卡片。**框架已封装**，直接 import 用：

```python
from gsuid_core.utils.html_render import (
    render_html_to_bytes,
    render_md_to_bytes,
    render_text_to_bytes,
)

async def render_report(stats: dict) -> bytes:
    md = f"""
# 今日早报

- 在线用户：**{stats['users']}**
- 今日查询：{stats['queries']}
- 错误数：{stats['errors']}
"""
    return await render_md_to_bytes(md=md, max_width=720)


async def render_dashboard(html: str) -> bytes:
    return await render_html_to_bytes(
        html,
        max_width=800,
        dpi=96,
        default_font_size=14,
        font_name="sans-serif",
        image_format="png",
        lang="zh",
    )
```

`pyproject.toml` 中显式声明依赖：
```toml
dependencies = ["pyrenderhtml>=0.0.5"]
```

## 9.4 playwright 范式（兜底）

只在 PIL / htmlkit 都不够用时才上 playwright（K 线 / 云图 / Plotly / ECharts 等）。
参考 `SayuStock/SayuStock/utils/image.py`：

```python
from pathlib import Path
from playwright.async_api import async_playwright
from gsuid_core.utils.image.convert import convert_img

async def render_image_by_pw(html_path: Path, w: int = 1920, h: int = 1080, scale: int = 2) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": w, "height": h},
            device_scale_factor=scale,
        )
        page = await context.new_page()
        await page.goto(html_path.absolute().as_uri())
        await page.wait_for_selector(".plot-container")     # 等关键元素就绪
        png_bytes = await page.screenshot(type="png")
        await browser.close()
        return await convert_img(png_bytes)
```

`pyproject.toml`：
```toml
dependencies = ["playwright>=1.49.0"]
```

**README 中必须显式说明**：
```
首次使用需运行：playwright install chromium
否则 launch() 会报"找不到 chromium"。
```

## 9.5 `convert_img`：所有发图前的最后一步

无论用哪种渲染方式，**最终发送给 `bot.send()` 的字节流都应过一次 `convert_img`**：

```python
from gsuid_core.utils.image.convert import convert_img

result = await convert_img(pil_image)   # PIL.Image / bytes / Path 都可以
await bot.send(result)
```

`convert_img` 会按框架配置做"是否转 base64""是否压缩""是否上传 RM"等统一处理，避免不同
平台适配器对裸 bytes 的兼容问题。
