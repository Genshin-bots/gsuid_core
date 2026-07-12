"""
Web Search 公共 API 模块

提供统一的 web 搜索接口，根据用户配置自动选择搜索引擎（Tavily / Exa / MCP）。
外部模块应通过本模块的函数调用搜索，无需关心底层搜索引擎的实现细节。
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.mcp.utils import (
    get_mcp_tool_id,
    is_mcp_provider,
    sanitize_mcp_text,
    build_mcp_arguments,
    call_mcp_tool_checked,
)
from gsuid_core.ai_core.configs.ai_config import ai_config

from .exa_search import exa_search
from .tavily_search import (
    tavily_search,
    tavily_search_with_context,
)

# 单条 MCP 原始返回兜底透传时的最大字符数，避免一次搜索把上下文吃满
_MAX_MCP_RAW_CHARS = 4000


def _get_provider() -> str:
    """
    获取当前配置的搜索引擎提供方

    Returns:
        搜索引擎名称，如 "Tavily"、"Exa" 或 "MCP"
    """
    return ai_config.get_config("websearch_provider").data


async def _mcp_search(query: str, max_results: int | None = None) -> list[dict]:
    """
    使用 MCP 进行 web 搜索

    Args:
        query: 搜索查询关键词
        max_results: 最大返回结果数量

    Returns:
        搜索结果列表
    """
    mcp_tool_id = get_mcp_tool_id("websearch_mcp_tool_id", "Web Search")

    arguments = build_mcp_arguments(
        "websearch_mcp_tool_id",
        {"query": query, "max_results": max_results},
    )

    result = await call_mcp_tool_checked(mcp_tool_id, arguments, "Web Search")

    return _parse_mcp_search_result(result.text, max_results)


def _parse_mcp_search_result(raw_text: str, max_results: int | None = None) -> list[dict]:
    """
    解析 MCP web search 返回的原始文本为结构化结果

    两段式策略（不同 MCP 返回格式差异极大，无法用一套 schema 归一）：
    1. 能 ``json.loads`` → 走结构化归一，按条干净截断（MiniMax 等）。
    2. 不能（知乎等返回 XML 化文本）→ **不再丢弃返回 []**，而是消毒（剥指令壳）
       + 限长后整段透传给模型当参考资料，由 LLM 自己阅读。

    Args:
        raw_text: MCP 返回的原始文本
        max_results: 最大结果数

    Returns:
        搜索结果列表
    """
    import json

    # 先消毒：剥掉 instruction 壳，避免间接 prompt injection
    cleaned = sanitize_mcp_text(raw_text)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # 非 JSON：兜底透传（限长），而不是返回空让模型误以为“搜不到”
        logger.debug(t("🌐 [WebSearch][MCP] 非 JSON 返回，按原文透传 ({p0} 字)", p0=len(cleaned)))
        return [
            {
                "title": "",
                "url": "",
                "content": sanitize_mcp_text(raw_text, max_chars=_MAX_MCP_RAW_CHARS),
                "score": 0.0,
            }
        ]

    # 调试日志：打印原始返回数据
    logger.debug(t("🌐 [WebSearch][MCP] 结构化返回: {p0}...", p0=cleaned[:500]))

    # 尝试解析为结果列表
    # MiniMax MCP 返回格式可能是 {"organic": [...]} 或 {"results": [...]} 或直接是 [...]
    if isinstance(data, list):
        results = data
    elif isinstance(data, dict):
        if "organic" in data:
            results = data["organic"]
        elif "results" in data:
            results = data["results"]
        else:
            results = [data]
    else:
        results = [data]

    # 限制结果数量
    if max_results is not None and len(results) > max_results:
        results = results[:max_results]

    # 标准化结果格式
    normalized: list[dict] = []
    for item in results:
        if isinstance(item, dict):
            normalized.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", item.get("snippet", "")),
                    "score": item.get("score", 0.0),
                }
            )
        else:
            normalized.append({"title": str(item), "url": "", "content": "", "score": 0.0})

    return normalized


async def web_search(
    query: str,
    max_results: int | None = None,
) -> list[dict]:
    """
    统一的 web 搜索接口

    根据用户配置的 websearch_provider 自动选择搜索引擎。

    Args:
        query: 搜索查询关键词
        max_results: 最大返回结果数量，默认由各搜索引擎配置决定

    Returns:
        搜索结果列表，每条包含 title、url、content、score 等字段

    Example:
        >>> results = await web_search("Python 教程")
        >>> for r in results:
        ...     print(r["title"], r["url"])
    """
    provider = _get_provider()

    if provider == "Exa":
        return await exa_search(query=query, max_results=max_results)

    if is_mcp_provider(provider):
        return await _mcp_search(query=query, max_results=max_results)

    # 默认使用 Tavily
    return await tavily_search(query=query, max_results=max_results)


async def web_search_with_context(
    query: str,
    max_results: int = 5,
) -> dict:
    """
    统一的带上下文 web 搜索接口

    根据用户配置的 websearch_provider 自动选择搜索引擎。
    该方法会同时返回搜索结果和 AI 生成的摘要答案（如果搜索引擎支持）。

    Args:
        query: 搜索查询关键词
        max_results: 最大返回结果数量，默认5条

    Returns:
        包含 results(结果列表) 和 answer(AI摘要) 的字典
    """
    provider = _get_provider()

    if provider == "Exa":
        results = await exa_search(query=query, max_results=max_results)
        return {"results": results, "answer": None}

    if is_mcp_provider(provider):
        results = await _mcp_search(query=query, max_results=max_results)
        return {"results": results, "answer": None}

    # 默认使用 Tavily
    return await tavily_search_with_context(query=query, max_results=max_results)
