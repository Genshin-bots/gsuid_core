"""
HTML渲染工具模块

提供将HTML或Markdown渲染为图片的能力，供AI调用。
"""

from typing import Literal

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.html_render import (
    render_md_to_bytes,
    render_html_to_bytes,
)


@ai_tools(category="media")
async def render_html_to_image(
    ctx: RunContext[ToolContext],
    html_content: str,
    image_format: Literal["png", "jpeg"] = "jpeg",
    max_width: int = 1000,
) -> bytes | str:
    """
    将HTML内容渲染为图片

    当用户说汇总成图片时、进行总结时、或者输出内容过于复杂需要使用图片给用户信息的时候，
    将AI生成的内容转为HTML代码渲染为精美的图片返回。
    该函数支持标准的HTML标签和内联样式（避免使用flex布局，兼容性更好）。

    Args:
        ctx: 工具执行上下文
        html_content: HTML内容，必须是完整的HTML文档（包含html/head/body标签）
        image_format: 图片格式，"png"或"jpeg"，默认"jpeg"
        max_width: 图片最大宽度，默认1000

    Returns:
        base64编码的图片数据，格式为 "base64://..."

    Example:
        >>> html = "<html><body><h1>Hello</h1></body></html>"
        >>> await render_html_to_image(ctx, html)
    """
    if not html_content or not html_content.strip():
        return "渲染失败：HTML内容不能为空"

    try:
        # 确保HTML有完整结构
        html = html_content.strip()
        if not html.startswith("<html"):
            # 简单的HTML包装
            html = (
                f"<!DOCTYPE html>\n<html>\n<head>\n"
                f'<meta charset="utf-8">\n'
                f"<style>body{{font-family:sans-serif;"
                f"padding:20px;}}</style>\n"
                f"</head>\n<body>\n{html}\n</body>\n</html>"
            )

        # 渲染HTML为图片字节
        image_bytes = await render_html_to_bytes(
            html,
            max_width=float(max_width),
            image_format=image_format,
        )

        # 转换为base64格式
        logger.info(t("🧠 [BuildinTools] HTML渲染成功，图片长度: {p0} bytes", p0=len(image_bytes)))

        return image_bytes

    except Exception as e:
        logger.exception(t("🧠 [BuildinTools] HTML渲染失败: {e}", e=e))
        return f"渲染失败：{str(e)}"


@ai_tools(category="media")
async def render_markdown_to_image(
    ctx: RunContext[ToolContext],
    markdown_content: str,
    image_format: Literal["png", "jpeg"] = "jpeg",
    max_width: int = 800,
) -> bytes | str:
    """
    将Markdown内容渲染为图片

    当用户说汇总成图片时、进行总结时、或者输出内容过于复杂需要使用图片给用户信息的时候，
    将Markdown文本渲染为精美的图片返回。
    支持标题、列表、代码块、链接等标准Markdown语法。

    Args:
        ctx: 工具执行上下文
        markdown_content: Markdown内容
        image_format: 图片格式，"png"或"jpeg"，默认"jpeg"
        max_width: 图片最大宽度，默认800

    Returns:
        base64编码的图片数据，格式为 "base64://..."

    Example:
        >>> md = "# Hello\\n\\n这是**加粗**文字"
        >>> await render_markdown_to_image(ctx, md)
    """
    if not markdown_content or not markdown_content.strip():
        return "渲染失败：Markdown内容不能为空"

    try:
        # 渲染Markdown为图片字节
        image_bytes = await render_md_to_bytes(
            md=markdown_content,
            max_width=max_width,
            image_format=image_format,
        )

        # 转换为base64格式
        logger.info(t("🧠 [BuildinTools] Markdown渲染成功，图片长度: {p0} bytes", p0=len(image_bytes)))

        return image_bytes

    except Exception as e:
        logger.exception(t("🧠 [BuildinTools] Markdown渲染失败: {e}", e=e))
        return f"渲染失败：{str(e)}"
