"""
网页抓取工具模块

提供网页内容抓取并转换为 Markdown 格式的功能，供 AI Agent 调用。
使用 aiohttp 进行异步 HTTP 请求，使用 markdownify 将 HTML 转换为 Markdown。
"""

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.web_fetch import fetch_webpage_as_markdown


@ai_tools(category="buildin")
async def web_fetch_tool(
    ctx: RunContext[ToolContext],
    url: str,
) -> str:
    """
    网页抓取工具

    当已经有一个具体网页 URL（通常来自 web_search_tool 的搜索结果），
    需要读取该网页的完整正文内容时使用。适合"看看这个链接""读一下这篇文章"
    "这个网址里写了什么"等场景。返回网页正文的 Markdown 文本。

    Args:
        ctx: 工具执行上下文
        url: 要抓取的网页 URL，必须以 http:// 或 https:// 开头

    Returns:
        网页内容的 Markdown 格式文本

    Example:
        >>> content = await web_fetch_tool(ctx, "https://example.com")
        >>> print(content)
    """
    try:
        result = await fetch_webpage_as_markdown(url=url)
        return result
    except ValueError as e:
        return f"抓取失败: {e}"
