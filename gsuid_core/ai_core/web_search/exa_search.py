"""
Exa Web Search 模块

提供基于 Exa API 的 web 搜索功能，支持语义搜索、关键词搜索等。
支持 api_key 池配置，实现自动轮询和失败重试。
"""

import random
from typing import Optional

from exa_py import AsyncExa

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import exa_config


def _get_api_key_pool() -> list[str]:
    """
    获取 api_key 池

    Returns:
        api_key 列表，如果是单个字符串则转换为单元素列表
    """
    api_key_data = exa_config.get_config("api_key").data

    if isinstance(api_key_data, list):
        return [k for k in api_key_data if k]
    elif isinstance(api_key_data, str) and api_key_data:
        return [api_key_data]
    else:
        return []


def _select_api_key(api_key_pool: list[str]) -> Optional[str]:
    """
    从 api_key 池中选择一个 api_key（随机选择）

    Args:
        api_key_pool: api_key 列表

    Returns:
        选中的 api_key，如果池为空则返回 None
    """
    if not api_key_pool:
        return None
    return random.choice(api_key_pool)


async def _do_exa_search(
    query: str,
    max_results: int,
    search_type: str,
    api_key: str,
) -> list[dict]:
    """
    执行 Exa 搜索的内部方法

    Args:
        query: 搜索查询关键词
        max_results: 最大返回结果数量
        search_type: 搜索类型，"neural"(语义搜索) 或 "keyword"(关键词搜索)
        api_key: Exa API Key

    Returns:
        搜索结果列表
    """
    client = AsyncExa(api_key=api_key)

    response = await client.search_and_contents(
        query=query,
        num_results=max_results,
        type=search_type,  # type: ignore
        text=True,
    )

    results = []
    for item in response.results:
        results.append(
            {
                "title": item.title or "",
                "url": item.url or "",
                "content": item.text or "",
                "score": item.score if item.score is not None else 0.0,
            }
        )

    return results


async def exa_search(
    query: str,
    max_results: Optional[int] = None,
) -> list[dict]:
    """
    使用 Exa API 进行 web 搜索

    支持 api_key 池配置，会自动轮询尝试不同的 api_key。

    Args:
        query: 搜索查询关键词
        max_results: 最大返回结果数量，默认10条

    Returns:
        搜索结果列表，每条包含 title、url、content、score 等字段

    Example:
        >>> results = await exa_search("Python 教程")
        >>> for r in results:
        ...     print(r["title"], r["url"])
    """
    api_key_pool = _get_api_key_pool()

    if not api_key_pool:
        logger.warning(t("🌐 [WebSearch] Exa API Key 未配置，跳过搜索"))
        return []

    if max_results is None:
        max_results = int(exa_config.get_config("max_results").data or "10")

    search_type = exa_config.get_config("search_type").data or "neural"

    # 记录已尝试的 api_key，避免重复尝试
    tried_keys = set()

    while len(tried_keys) < len(api_key_pool):
        api_key = _select_api_key([k for k in api_key_pool if k not in tried_keys])
        if not api_key:
            break

        tried_keys.add(api_key)

        try:
            results = await _do_exa_search(
                query=query,
                max_results=max_results,
                search_type=search_type,
                api_key=api_key,
            )

            logger.info(t("🌐 [WebSearch][Exa] 搜索: {query}, 返回 {p0} 条结果", query=query, p0=len(results)))
            return results

        except Exception:
            logger.warning(t("🌐 [WebSearch][Exa] api_key ...{p0} 失败，尝试下一个", p0=api_key[-4:]))
            continue

    logger.error(t("🌐 [WebSearch][Exa] 所有 api_key 均失败"))
    return []
