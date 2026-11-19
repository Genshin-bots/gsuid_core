"""
Tavily Web Search 模块

通过 httpx2 直调 Tavily REST API（POST https://api.tavily.com/search），
不加载第三方 tavily-python SDK；支持 api_key 池轮询与失败重试。
"""

from __future__ import annotations

import random

import httpx2

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import tavily_config

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_TAVILY_TIMEOUT_SEC = 30.0
_IMG_CAP = 6


def _get_api_key_pool() -> list[str]:
    """获取 api_key 池，单串转为单元素列表。"""
    api_key_data = tavily_config.get_config("api_key").data

    if isinstance(api_key_data, list):
        return [k for k in api_key_data if isinstance(k, str) and k]
    if isinstance(api_key_data, str) and api_key_data:
        return [api_key_data]
    return []


def _select_api_key(api_key_pool: list[str]) -> str | None:
    if not api_key_pool:
        return None
    return random.choice(api_key_pool)


def _str_field(item: dict[str, object], key: str) -> str:
    if key not in item or item[key] is None:
        return ""
    return str(item[key])


def _float_field(item: dict[str, object], key: str) -> float:
    if key not in item:
        return 0.0
    val = item[key]
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    return 0.0


def _parse_results(response: dict[str, object]) -> list[dict[str, str | float]]:
    results: list[dict[str, str | float]] = []
    raw_items = response["results"] if "results" in response else None
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            published = _str_field(item, "published_date").strip()
            results.append(
                {
                    "title": _str_field(item, "title"),
                    "url": _str_field(item, "url"),
                    "content": _str_field(item, "content"),
                    "published_date": published,
                    "score": _float_field(item, "score"),
                }
            )

    img_n = 0
    raw_images = response["images"] if "images" in response else None
    if isinstance(raw_images, list):
        for img_item in raw_images:
            if img_n >= _IMG_CAP:
                break
            if isinstance(img_item, str):
                raw_url = img_item
            elif isinstance(img_item, dict) and "url" in img_item and isinstance(img_item["url"], str):
                raw_url = img_item["url"]
            else:
                continue
            u = raw_url.strip()
            if not u.startswith(("http://", "https://")):
                continue
            results.append(
                {
                    "title": "(配图)",
                    "url": u,
                    "content": "",
                    "published_date": "",
                    "score": 0.0,
                    "image_url": u,
                    "kind": "image",
                }
            )
            img_n += 1

    return results


async def _post_tavily_search(
    query: str,
    max_results: int,
    search_depth: str,
    api_key: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": True,
        "include_raw_content": False,
        "include_images": True,
        "include_published_date": True,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(_TAVILY_TIMEOUT_SEC)) as client:
        resp = await client.post(_TAVILY_SEARCH_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data: object = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Tavily 响应非 JSON 对象")
    out: dict[str, object] = {}
    for key, val in data.items():
        if isinstance(key, str):
            out[key] = val
    return out


async def _do_tavily_search(
    query: str,
    max_results: int,
    search_depth: str,
    api_key: str,
) -> list[dict[str, str | float]]:
    response = await _post_tavily_search(query, max_results, search_depth, api_key)
    return _parse_results(response)


async def _do_tavily_search_with_context(
    query: str,
    max_results: int,
    api_key: str,
) -> dict[str, object]:
    response = await _post_tavily_search(query, max_results, "advanced", api_key)
    results = _parse_results(response)
    answer = response["answer"] if "answer" in response else None
    return {"results": results, "answer": answer}


async def tavily_search(
    query: str,
    max_results: int | None = None,
) -> list[dict[str, str | float]]:
    """使用 Tavily API 进行 web 搜索，支持 api_key 池轮询重试。"""
    api_key_pool = _get_api_key_pool()

    if not api_key_pool:
        logger.warning(t("log.ai.websearch_tavily_api_key_skip"))
        raise RuntimeError(t("log.ai.websearch_tavily_api_key_skip"))

    if max_results is None:
        max_results = int(tavily_config.get_config("max_results").data or "10")

    search_depth = str(tavily_config.get_config("search_depth").data or "advanced")

    tried_keys: set[str] = set()
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

        except (httpx2.HTTPError, RuntimeError, OSError, ValueError, TypeError) as e:
            logger.warning(t("log.ai.websearch_tavily_api_key_trying", p0=api_key[-4:]))
            last_err = e
            continue

    logger.error(t("log.ai.websearch_tavily_api_keys"))
    raise RuntimeError(t("log.ai.websearch_tavily_api_keys") + (f": {last_err}" if last_err else ""))


async def tavily_search_with_context(
    query: str,
    max_results: int = 5,
) -> dict[str, object]:
    """使用 Tavily API 进行带上下文的搜索，返回 results 与 answer。"""
    api_key_pool = _get_api_key_pool()

    if not api_key_pool:
        logger.warning(t("log.ai.websearch_tavily_api_key_skip"))
        raise RuntimeError(t("log.ai.websearch_tavily_api_key_skip"))

    tried_keys: set[str] = set()
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
            raw_res = result["results"] if "results" in result else []
            res_count = len(raw_res) if isinstance(raw_res, list) else 0
            logger.info(
                t(
                    "log.ai.websearch_tavily_search_context",
                    query=query,
                    p0=res_count,
                )
            )
            return result

        except (httpx2.HTTPError, RuntimeError, OSError, ValueError, TypeError) as e:
            logger.warning(t("log.ai.websearch_tavily_api_key_trying", p0=api_key[-4:]))
            last_err = e
            continue

    logger.error(t("log.ai.websearch_tavily_api_keys"))
    raise RuntimeError(t("log.ai.websearch_tavily_api_keys") + (f": {last_err}" if last_err else ""))
