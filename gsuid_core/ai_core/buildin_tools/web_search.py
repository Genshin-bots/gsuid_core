"""
Web搜索工具模块

提供统一的 web 搜索功能，供 AI Agent 调用。
根据用户配置自动选择搜索引擎（Tavily / Exa）。
"""

from pydantic_ai import RunContext

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.web_search import web_search


@ai_tools(category="buildin")
async def web_search_tool(
    ctx: RunContext[ToolContext],
    query: str,
    limit: int = 10,
) -> str:
    """
    Web搜索工具

    当需要查询实时信息、最新消息、当前价格、近期事件、今日/本周/本月发生的事情，
    或遇到任何不确定、不了解的话题时使用。适合"最新""现在""今天""最近""怎么了"
    "是什么""出了什么事"这类时效性或开放性问题，也可作为没有专属工具时的兜底查询。
    返回搜索引擎的结果摘要列表。

    Args:
        ctx: 工具执行上下文
        query: 搜索查询关键词，如"最新的科技新闻"或"Python 教程"
        limit: 最大返回结果数量，默认10条

    Returns:
        搜索结果列表字符串

    Example:
        >>> results = await web_search_tool(ctx, "原神 4.0 更新内容")
        >>> print(results)
    """
    results = await web_search(
        query=query,
        max_results=limit,
    )
    return str(results)
