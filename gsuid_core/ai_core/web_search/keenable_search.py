"""
Keenable Web Search 模块

提供基于 Keenable Search API 的 web 搜索。API Key 可选：未填写时走
公开端点 /v1/search/public，并带上该端点要求的 X-Keenable-Title；
填写 KEENABLE_API_KEY（或本模块配置中的 api_key）时走 /v1/search。
默认不会进入搜索链，只有把主用或备用源设为 Keenable 时才会调用。
"""

from __future__ import annotations

import random
from typing import Optional

import aiohttp

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import keenable_config

PUBLIC_SEARCH_URL = "https://api.keenable.ai/v1/search/public"
KEYED_SEARCH_URL = "https://api.keenable.ai/v1/search"
APP_TITLE = "gsuid_core"


class KeenableSearchError(RuntimeError):
    """Keenable 搜索失败。"""


def _get_api_key_pool() -> list[str]:
    api_key_data = keenable_config.get_config("api_key").data
    if isinstance(api_key_data, list):
        return [k for k in api_key_data if k]
    if isinstance(api_key_data, str) and api_key_data:
        return [api_key_data]
    return []


def _select_api_key(api_key_pool: list[str]) -> Optional[str]:
    if not api_key_pool:
        return None
    return random.choice(api_key_pool)


def _max_results_default() -> int:
    try:
        return int(keenable_config.get_config("max_results").data or 10)
    except (TypeError, ValueError):
        return 10


def _timeout_sec() -> int:
    try:
        return int(keenable_config.get_config("timeout").data or 20)
    except (TypeError, ValueError):
        return 20


def _normalize_items(payload: object, max_results: int) -> list[dict]:
    items = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url") or item.get("link") or ""
        if not url:
            continue
        published = ""
        if item.get("published_date") is not None:
            published = str(item.get("published_date")).strip()
        elif item.get("published") is not None:
            published = str(item.get("published")).strip()
        score = item.get("score", 0.0)
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0
        out.append(
            {
                "title": item.get("title") or "",
                "url": url,
                "content": item.get("snippet") or item.get("content") or item.get("description") or "",
                "published_date": published,
                "score": score,
            }
        )
        if len(out) >= max_results:
            break
    return out


async def _do_keenable_search(query: str, max_results: int, api_key: Optional[str]) -> list[dict]:
    url = KEYED_SEARCH_URL if api_key else PUBLIC_SEARCH_URL
    headers = {
        "Content-Type": "application/json",
        "X-Keenable-Title": APP_TITLE,
        "User-Agent": "gsuid-core-keenable-search/1.0",
    }
    if api_key:
        headers["X-API-Key"] = api_key
    timeout = aiohttp.ClientTimeout(total=_timeout_sec())
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url,
            json={"query": query, "max_results": max_results},
            headers=headers,
        ) as resp:
            body = await resp.text()
            if resp.status in (401, 403):
                raise KeenableSearchError(f"Keenable auth HTTP {resp.status}: {body[:300]}")
            if resp.status == 429:
                raise KeenableSearchError(f"Keenable rate limit HTTP 429: {body[:300]}")
            if resp.status >= 400:
                raise KeenableSearchError(f"Keenable search HTTP {resp.status}: {body[:300]}")
            try:
                data = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError, TypeError) as e:
                raise KeenableSearchError(f"Keenable returned a non-JSON body: {body[:300]}") from e
            return _normalize_items(data, max_results)


async def keenable_search(
    query: str,
    max_results: Optional[int] = None,
) -> list[dict]:
    """使用 Keenable Search。无 Key 时走公开端点。"""
    if max_results is None:
        max_results = _max_results_default()

    api_key_pool = _get_api_key_pool()
    if not api_key_pool:
        results = await _do_keenable_search(query, max_results, None)
        logger.info(t("log.ai.websearch_keenable_search_query", query=query, p0=len(results)))
        return results

    tried: set[str] = set()
    last_err: Optional[Exception] = None
    while len(tried) < len(api_key_pool):
        api_key = _select_api_key([k for k in api_key_pool if k not in tried])
        if not api_key:
            break
        tried.add(api_key)
        try:
            results = await _do_keenable_search(query, max_results, api_key)
            logger.info(t("log.ai.websearch_keenable_search_query", query=query, p0=len(results)))
            return results
        except (KeenableSearchError, aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
            last_err = e
            logger.warning(t("log.ai.websearch_keenable_api_key_trying", p0=api_key[-4:]))
            continue

    msg = t("log.ai.websearch_keenable_api_keys")
    if last_err:
        raise KeenableSearchError(f"{msg}: {last_err}") from last_err
    raise KeenableSearchError(msg)


def keenable_search_configured() -> bool:
    """Keenable 可 keyless 调用，选作主用/备用即视为已配置。"""
    return True
