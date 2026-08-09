"""
Tavily Web Search 模块

提供基于 Tavily API 的 web 搜索功能，支持通用搜索、新闻搜索、图片搜索等。
支持 api_key 池配置，实现自动轮询和失败重试。
"""

import random
from typing import Optional

from tavily import TavilyClient

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import tavily_config


def _get_api_key_pool() -> list[str]:
    """
    获取 api_key 池

    Returns:
        api_key 列表，如果是单个字符串则转换为单元素列表
    """
    api_key_data = tavily_config.get_config("api_key").data

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


async def _do_tavily_search(
    query: str,
    max_results: int,
    search_depth: str,
    api_key: str,
) -> list[dict]:
    """
    执行 Tavily 搜索的内部方法

    Args:
        query: 搜索查询关键词
        max_results: 最大返回结果数量
        search_depth: 搜索深度
        api_key: Tavily API Key

    Returns:
        搜索结果列表
    """
    client = TavilyClient(api_key=api_key)

    response = client.search(
        query=query,
        max_results=max_results,
        search_depth=search_depth,  # type: ignore
        include_answer=True,
        include_raw_content=False,
        include_images=True,
    )

    results = []
    for item in response.get("results", []):
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
                "score": item.get("score", 0.0),
            }
        )

    # 配图 URL 单独附在结果尾（供 research → render 嵌进 HTML）；限量防刷 token
    _img_n = 0
    for img_url in response.get("images") or []:
        if _img_n >= 6:
            break
        if not isinstance(img_url, str):
            continue
        u = img_url.strip()
        if not u.startswith(("http://", "https://")):
            continue
        results.append(
            {
                "title": "(配图)",
                "url": u,
                "content": "",
                "score": 0.0,
                "image_url": u,
                "kind": "image",
            }
        )
        _img_n += 1

    return results


async def _do_tavily_search_with_context(
    query: str,
    max_results: int,
    api_key: str,
) -> dict:
    """
    执行带上下文的 Tavily 搜索的内部方法

    Args:
        query: 搜索查询关键词
        max_results: 最大返回结果数量
        api_key: Tavily API Key

    Returns:
        包含 results(结果列表) 和 answer(AI摘要) 的字典
    """
    client = TavilyClient(api_key=api_key)
    response = client.search(
        query=query,
        max_results=max_results,
        search_depth="advanced",  # type: ignore
        include_answer=True,
        include_raw_content=False,
        include_images=True,
    )

    results = []
    for item in response.get("results", []):
        results.append(
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "content": item.get("content", ""),
                "score": item.get("score", 0.0),
            }
        )

    _img_n = 0
    for img_url in response.get("images") or []:
        if _img_n >= 6:
            break
        if not isinstance(img_url, str):
            continue
        u = img_url.strip()
        if not u.startswith(("http://", "https://")):
            continue
        results.append(
            {
                "title": "(配图)",
                "url": u,
                "content": "",
                "score": 0.0,
                "image_url": u,
                "kind": "image",
            }
        )
        _img_n += 1

    answer = response.get("answer")

    return {"results": results, "answer": answer}


async def tavily_search(
    query: str,
    max_results: Optional[int] = None,
) -> list[dict]:
    """
    使用 Tavily API 进行 web 搜索

    支持 api_key 池配置，会自动轮询尝试不同的 api_key。

    Args:
        query: 搜索查询关键词
        max_results: 最大返回结果数量，默认10条

    Returns:
        搜索结果列表，每条包含 title、url、content、score 等字段

    Example:
        >>> results = await tavily_search("Python 教程")
        >>> for r in results:
        ...     print(r["title"], r["url"])
    """
    api_key_pool = _get_api_key_pool()

    if not api_key_pool:
        logger.warning(t("log.ai.websearch_tavily_api_key_skip"))
        raise RuntimeError(t("log.ai.websearch_tavily_api_key_skip"))

    if max_results is None:
        max_results = int(tavily_config.get_config("max_results").data or "10")

    search_depth = tavily_config.get_config("search_depth").data or "advanced"

    tried_keys = set()
    last_err: Exception | None = None

    while len(tried_keys) < len(api_key_pool):
        api_key = _select_api_key([k for k in api_key_pool if k not in tried_keys])
        if not api_key:
            break

        tried_keys.add(api_key)

        try:
            results = await _do_tavily_search(
                query=query,
                max_results=max_results,
                search_depth=search_depth,
                api_key=api_key,
            )

            logger.info(t("log.ai.websearch_tavily_search_query", query=query, p0=len(results)))
            return results

        except (RuntimeError, OSError, ValueError, TypeError) as e:
            logger.warning(t("log.ai.websearch_tavily_api_key_trying", p0=api_key[-4:]))
            last_err = e
            continue

    logger.error(t("log.ai.websearch_tavily_api_keys"))
    # 抛出以便 web_search 多源策略可切换到下一源（额度用尽/鉴权失败）
    raise RuntimeError(t("log.ai.websearch_tavily_api_keys") + (f": {last_err}" if last_err else ""))


async def tavily_search_with_context(
    query: str,
    max_results: int = 5,
) -> dict:
    """
    使用 Tavily API 进行带上下文的搜索

    支持 api_key 池配置，会自动轮询尝试不同的 api_key。
    该方法会同时返回搜索结果和 AI 生成的摘要答案。

    Args:
        query: 搜索查询关键词
        max_results: 最大返回结果数量，默认5条

    Returns:
        包含 results(结果列表) 和 answer(AI摘要) 的字典
    """
    api_key_pool = _get_api_key_pool()

    if not api_key_pool:
        logger.warning(t("log.ai.websearch_tavily_api_key_skip"))
        raise RuntimeError(t("log.ai.websearch_tavily_api_key_skip"))

    tried_keys = set()
    last_err: Exception | None = None

    while len(tried_keys) < len(api_key_pool):
        api_key = _select_api_key([k for k in api_key_pool if k not in tried_keys])
        if not api_key:
            break

        tried_keys.add(api_key)

        try:
            result = await _do_tavily_search_with_context(
                query=query,
                max_results=max_results,
                api_key=api_key,
            )

            logger.info(
                t(
                    "log.ai.websearch_tavily_search_context",
                    query=query,
                    p0=len(result["results"]),
                )
            )
            return result

        except (RuntimeError, OSError, ValueError, TypeError) as e:
            logger.warning(t("log.ai.websearch_tavily_api_key_trying", p0=api_key[-4:]))
            last_err = e
            continue

    logger.error(t("log.ai.websearch_tavily_api_keys"))
    raise RuntimeError(t("log.ai.websearch_tavily_api_keys") + (f": {last_err}" if last_err else ""))
