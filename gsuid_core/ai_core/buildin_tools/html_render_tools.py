"""HTML渲染工具模块

提供将HTML或Markdown渲染为图片的能力，供AI调用。
渲染成功后自动通过 bot 发送图片，返回文本确认（无需再调 send_message_by_ai）。
"""

import datetime
from typing import Literal
from pathlib import Path

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.html_render import (
    render_md_to_bytes,
    render_html_to_bytes,
)

_MD_CSS_PATH = str(Path(__file__).resolve().parent.parent.parent / "utils" / "html_render" / "markdown_dark.css")

_FOOTER_TEMPLATE = "\n\n---\n\n> AI 生成资料 · 数据可能滞后 · 仅供参考 · {ts}"

# 暗色可视化壳：body 零 padding，间距由内容元素 margin 控制（content-box 安全）
_HTML_SHELL = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
*{{margin:0;padding:0;}}
body{{
  width:800px;overflow:hidden;
  background:
    radial-gradient(ellipse 70% 50% at 15% 0%,rgba(91,157,217,0.07) 0%,transparent 55%),
    radial-gradient(ellipse 50% 40% at 85% 95%,rgba(62,201,167,0.05) 0%,transparent 50%),
    linear-gradient(175deg,#080d18 0%,#0d1424 50%,#0a1020 100%);
  font-family:"MiSans","PingFang SC","Microsoft YaHei",sans-serif;
  color:#edf2fa;line-height:1.5;
}}
</style></head>
<body>
{body}
<div style="margin:20px 32px 24px;padding-top:12px;border-top:1px solid rgba(91,157,217,0.08);
  font-size:10px;color:#3d5068;letter-spacing:0.03em;">
  AI 生成资料 · 数据可能滞后 · 仅供参考 · {ts}</div>
</body></html>"""


def _footer() -> str:
    return _FOOTER_TEMPLATE.format(ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))


def _wrap_html_if_needed(html_content: str) -> str:
    """缺完整文档结构时套暗色壳。"""
    html = html_content.strip()
    lower = html[:200].lower()
    if lower.startswith("<!doctype") or lower.startswith("<html"):
        return html
    return _HTML_SHELL.format(
        body=html,
        ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


async def _try_send_image(ctx: RunContext[ToolContext], image_bytes: bytes) -> bool:
    """尝试通过 bot 直接发送图片。成功返回 True。"""
    bot = ctx.deps.bot
    if bot is None:
        return False
    try:
        await bot.send(MessageSegment.image(image_bytes))
        return True
    except Exception as e:
        logger.debug(f"渲染图片自动发送失败，回退资源注册: {e}")
        return False


@ai_tools(category="buildin")
async def render_html_to_image(
    ctx: RunContext[ToolContext],
    html_content: str,
    image_format: Literal["png", "jpeg"] = "png",
    max_width: int = 800,
) -> str:
    r"""
    将 HTML 渲染为高清图片并自动发送给用户。调用即发图，返回文本确认。

    ## 视觉设计指南（必读——决定出图质量）

    你写的 HTML 会被渲染成一张发给用户的图片。目标：像专业信息图/数据面板，
    而非"把文字截图"。以下是核心技法：

    ### 1. 画布与防溢出（⚠️ 最易踩坑）
    - body 已固定 `width:800px; overflow:hidden`。你写片段即可，系统套壳。
    - 若写完整 HTML，body 必须设 `width:800px; overflow:hidden`。
    - **引擎用 content-box 模型**（不支持 box-sizing:border-box）！
      `width` 只设内容宽，padding/border 会额外撑大元素。
      计算：实际占宽 = width + padding-left + padding-right + border-left + border-right。
    - 用 margin（非 padding）做外边距。flex 子项设 width 时要扣除自身 padding+border。
    - **总宽校验**：所有同级元素实际占宽 + gap 之和 ≤ 800px - 左右 margin。
    - 例：3 卡片 margin:0 32px → 可用 736px。每卡 padding:14px border:1px → 额外 30px。
      设 width:207px → 实际 237px。3×237 + 2×10(gap) = 731 ≤ 736 ✓

    ### 2. 层次感（让画面"活"起来）
    - body 壳已带径向渐变环境光。你在内容区可叠加：
      `background: radial-gradient(circle at 80% 20%, rgba(91,157,217,0.06), transparent 60%);`
    - 卡片用 `box-shadow: 0 4px 24px rgba(0,0,0,0.4)` 制造浮起感。
    - 装饰性光斑：`position:absolute` 的模糊圆（`border-radius:50%; filter:blur(40px); opacity:0.15`）。

    ### 3. 图标组件（禁 emoji，用 CSS 画）
    ```html
    <div style="width:44px;height:44px;border-radius:22px;flex:none;
      display:flex;align-items:center;justify-content:center;
      font-size:16px;font-weight:900;color:#0a1220;
      background:linear-gradient(135deg,#5b9dd9,#3a7ab8);
      box-shadow:0 3px 12px rgba(91,157,217,0.35);">雷</div>
    ```
    渐变配色参考：蓝#5b9dd9 绿#3ec9a7 金#e8b45a 红#ef7d6c 紫#9d8cff 灰#8fa3bf

    ### 4. 数据卡片（指标面板）
    ```html
    <div style="flex:1;background:linear-gradient(160deg,#1a2740,#131d30);
      border:1px solid rgba(91,157,217,0.12);border-radius:16px;
      padding:18px 16px;border-top:3px solid #5b9dd9;
      box-shadow:0 4px 20px rgba(0,0,0,0.35);">
      <div style="font-size:11px;color:#6b8299;font-weight:700;letter-spacing:0.1em;">标签</div>
      <div style="font-size:36px;font-weight:900;color:#fff;margin:8px 0 4px;">33°</div>
      <div style="font-size:12px;color:#4fe0b8;font-weight:700;">副文本</div>
    </div>
    ```

    ### 5. 数据行（列表/新闻/要点）
    ```html
    <div style="display:flex;align-items:center;gap:14px;
      background:linear-gradient(135deg,#162035,#111a2c);
      border:1px solid rgba(91,157,217,0.08);border-radius:14px;
      padding:14px 16px;margin-top:10px;box-shadow:0 2px 12px rgba(0,0,0,0.2);">
      <!-- 图标 --> <!-- 中间内容 flex:1 --> <!-- 右侧标签 -->
    </div>
    ```

    ### 6. 排版对比（制造视觉冲击）
    - 关键数字：36-44px, font-weight:900, color:#fff
    - 标题：22-26px, font-weight:800
    - 正文/描述：13-14px, color:#8a9bb5
    - 标签/脚注：11px, letter-spacing:0.08em, color:#4a5d75
    - 行间距：标题 margin-bottom:6px，段落 margin-top:20px

    ### 7. 色条/分割/装饰
    - 顶部色条：`height:4px; background:linear-gradient(90deg,#5b9dd9,#3ec9a7,#e8b45a,#ef7d6c);`
    - 节标题：`border-left:4px solid #5b9dd9; padding-left:12px; font-weight:800;`
    - 分割线：`border-top:1px solid rgba(91,157,217,0.08);`

    ### 8. 背景图（支持 base64 data URI 和 URL）
    ```html
    <div style="position:relative;width:100%;height:200px;border-radius:16px;overflow:hidden;">
      <img src="data:image/png;base64,..." style="width:100%;height:100%;object-fit:cover;opacity:0.3;">
      <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;">
        <!-- 前景内容 -->
      </div>
    </div>
    ```

    ### 禁止事项
    - **禁止 `<table>`**（引擎不支持，变纯文本墙）
    - **禁止 emoji**（字体缺字变空白）
    - **禁止子元素总宽超 800px**（会裁切）
    - **禁止纯文字堆砌**——每块内容必须有视觉容器（卡片/行/图标）

    ### 引擎能力
    ✅ flex + position:absolute/relative + z-index
    ✅ background-image / <img>（base64 或 URL）
    ✅ linear-gradient / radial-gradient
    ✅ box-shadow / border-radius / overflow:hidden
    ✅ filter:blur() / opacity
    ❌ JavaScript / 外链 CSS / @import

    Args:
        ctx: 工具执行上下文
        html_content: HTML body 片段（推荐）或完整 HTML 文档
        image_format: 默认 png
        max_width: 固定 800，不要改

    Returns:
        文本确认（图片已自动发送）
    """
    if not html_content or not html_content.strip():
        return "渲染失败：HTML内容不能为空"

    try:
        html = _wrap_html_if_needed(html_content)
        # pytakumi max_width = 物理像素；CSS 视口 = max_width / dpr
        # agent 传入的 max_width 是 CSS 像素，需乘 dpr 得到物理像素
        _dpr = 2.0
        image_bytes = await render_html_to_bytes(
            html,
            max_width=float(max_width) * _dpr,
            dpi=96.0 * _dpr,
            image_format=image_format,
            default_font_size=15.0,
        )
        logger.info(t("log.ai.buildintools_html_rendering_succeeded_ok", p0=len(image_bytes)))

        if await _try_send_image(ctx, image_bytes):
            return f"图片已发送（{len(image_bytes) // 1024}KB）。台词只留一两句引导，禁止复述数据。"
        return image_bytes  # type: ignore[return-value]
    except Exception as e:
        logger.exception(t("log.ai.buildintools_html_rendering", e=e))
        return f"渲染失败：{str(e)}"


@ai_tools(category="buildin")
async def render_markdown_to_image(
    ctx: RunContext[ToolContext],
    title: str,
    markdown_content: str,
    image_format: Literal["png", "jpeg"] = "png",
    max_width: int = 800,
) -> str:
    """
    将 Markdown 渲染为暗色卡片图片并自动发送。

    适合纯文字列表/步骤/简单表格。需要可视化布局（指标卡/图标行/背景图）时用 render_html_to_image。
    ≥3 条数据点时必须出图；角色台词只留一两句引导。

    Args:
        ctx: 工具执行上下文
        title: 图片标题
        markdown_content: Markdown 正文（不含一级标题）
        image_format: 默认 png
        max_width: 默认 800

    Returns:
        文本确认（图片已自动发送）
    """
    if not markdown_content or not markdown_content.strip():
        return "渲染失败：内容不能为空"

    try:
        md = f"# {title}\n\n{markdown_content.strip()}{_footer()}"
        _dpr = 2.0
        image_bytes = await render_md_to_bytes(
            md=md,
            css_path=_MD_CSS_PATH,
            max_width=int(max_width * _dpr),
            dpi=96.0 * _dpr,
            image_format=image_format,
            dark=False,
        )
        logger.info(t("log.ai.buildintools_markdown_rendering_succeeded_ok", p0=len(image_bytes)))

        if await _try_send_image(ctx, image_bytes):
            return f"图片已发送（{len(image_bytes) // 1024}KB）。台词只留一两句引导，禁止复述数据。"
        return image_bytes  # type: ignore[return-value]
    except Exception as e:
        logger.exception(t("log.ai.buildintools_markdown_rendering", e=e))
        return f"渲染失败：{str(e)}"
