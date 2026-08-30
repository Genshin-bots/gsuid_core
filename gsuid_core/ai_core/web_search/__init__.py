"""
Web Search 模块

提供统一的 web 搜索接口，支持 Tavily / Jina / Exa / AnySearch / Firecrawl / MCP 等搜索引擎。
外部模块应通过 search 模块的公共 API 调用搜索。
"""

from gsuid_core.ai_core.web_search.search import web_search, web_search_with_context

__all__ = ["web_search", "web_search_with_context"]
