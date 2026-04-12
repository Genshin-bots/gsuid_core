"""
Web搜索工具模块

提供基于 Tavily API 的 web 搜索功能，供 AI Agent 调用。
"""

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.web_search import tavily_search


@ai_tools(category="buildin")
async def web_search(
    ctx: RunContext[ToolContext],
    query: str,
    limit: int = 10,
) -> str:
    """
    Web搜索工具

    使用 Tavily API 进行 web 搜索，返回搜索结果列表。

    Args:
        ctx: 工具执行上下文
        query: 搜索查询关键词，如"最新的科技新闻"或"Python 教程"
        limit: 最大返回结果数量，默认10条

    Returns:
        搜索结果列表字符串

    Example:
        >>> results = await web_search(ctx, "原神 4.0 更新内容")
        >>> print(results)
    """
    results = await tavily_search(
        query=query,
        max_results=limit,
    )
    return str(results)
