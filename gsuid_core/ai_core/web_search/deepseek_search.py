"""
DeepSeek Web Search（POST https://api.deepseek.com/v1/tools/search）

文档：https://api-docs.deepseek.com/zh-cn/guides/search/
鉴权：Authorization: Bearer <api_key>（必填）
"""

from __future__ import annotations

import json
import random
from typing import TypedDict

import aiohttp

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import deepseek_config

_DEFAULT_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
_CLIENT_HEADER = "gsuid-core-deepseek/1.0"
_MAX_QUERY_LEN = 128
_MAX_RESULTS_CAP = 50


class DeepSeekSearchError(RuntimeError):
    """DeepSeek 搜索失败（额度/鉴权/网络等），供上层多源切换捕获。"""


class _SearchBody(TypedDict, total=False):
    query: str
    count: int
    need_content: bool


def _get_api_key_pool() -> list[str]:
    api_key_data = deepseek_config.get_config("api_key").data
    if isinstance(api_key_data, list):
        return [k for k in api_key_data if isinstance(k, str) and k]
    if isinstance(api_key_data, str) and api_key_data:
        return [api_key_data]
    return []


def _select_api_key(api_key_pool: list[str]) -> str | None:
    if not api_key_pool:
        return None
    return random.choice(api_key_pool)


def _as_int(raw: object, default: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return raw


def _timeout_sec() -> int:
    n = _as_int(deepseek_config.get_config("timeout").data, 30)
    if n < 5:
        return 5
    if n > 120:
        return 120
    return n


def _search_base_url() -> str:
    raw = deepseek_config.get_config("search_base_url").data
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return _DEFAULT_SEARCH_URL


def _need_content() -> bool:
    raw = deepseek_config.get_config("need_content").data
    return raw is True


def _clamp_max_results(n: int) -> int:
    if n < 1:
        return 1
    if n > _MAX_RESULTS_CAP:
        return _MAX_RESULTS_CAP
    return n


def _clamp_query(query: str) -> str:
    s = query.strip()
    if len(s) > _MAX_QUERY_LEN:
        return s[:_MAX_QUERY_LEN]
    return s


def _first_str(item: dict[str, object], *keys: str) -> str:
    for key in keys:
        if key in item and item[key] is not None:
            val = str(item[key]).strip()
            if val:
                return val
    return ""


def _parse_score(item: dict[str, object]) -> float:
    if "rerank_score" in item:
        raw = item["rerank_score"]
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw)
            except ValueError:
                return 0.0
    return 0.0


def _extract_content(item: dict[str, object], need_content: bool) -> str:
    keys = ("content", "summary", "snippet") if need_content else ("summary", "snippet", "content")
    return _first_str(item, *keys)


def _normalize_results(data: object, max_results: int, need_content: bool) -> list[dict[str, str | float]]:
    """将 DeepSeek JSON 返回归一为标准列表：title/url/content/score。"""
    items: list[object]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        if "results" in data and isinstance(data["results"], list):
            items = data["results"]
        else:
            items = [data]
    else:
        items = []

    out: list[dict[str, str | float]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _first_str(item, "title")
        url = _first_str(item, "url")
        content = _extract_content(item, need_content)
        if not title and not url and not content:
            continue
        score = _parse_score(item)
        published = _first_str(item, "date")
        out.append(
            {
                "title": title,
                "url": url,
                "content": content,
                "published_date": published,
                "score": score,
            }
        )
        if len(out) >= max_results:
            break
    return out


async def _do_deepseek_search(
    query: str,
    max_results: int,
    api_key: str,
    need_content: bool,
) -> list[dict[str, str | float]]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": _CLIENT_HEADER,
        "Authorization": f"Bearer {api_key}",
    }

    body: _SearchBody = {
        "query": _clamp_query(query),
        "count": max_results,
        "need_content": need_content,
    }

    url = _search_base_url()
    timeout = aiohttp.ClientTimeout(total=_timeout_sec())
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=body, headers=headers) as resp:
            text = await resp.text()
            if resp.status in (401, 403):
                raise DeepSeekSearchError(f"DeepSeek auth HTTP {resp.status}: {text[:300]}")
            if resp.status == 429:
                raise DeepSeekSearchError(f"DeepSeek rate limit HTTP 429: {text[:300]}")
            if resp.status >= 400:
                raise DeepSeekSearchError(f"DeepSeek search HTTP {resp.status}: {text[:300]}")

            payload: object
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError as err:
                raise DeepSeekSearchError(f"DeepSeek invalid JSON: {text[:200]}") from err

            return _normalize_results(payload, max_results, need_content)


async def deepseek_search(
    query: str,
    max_results: int | None = None,
) -> list[dict[str, str | float]]:
    """
    使用 DeepSeek 搜索 API 进行 web 搜索。

    Key 池非空时轮询重试。全部失败则抛出 DeepSeekSearchError。
    """
    api_key_pool = _get_api_key_pool()
    if not api_key_pool:
        raise DeepSeekSearchError(t("log.ai.websearch_deepseek_api_key_skip"))

    if max_results is None:
        max_results = _as_int(deepseek_config.get_config("max_results").data, 10)
    max_results = _clamp_max_results(max_results)
    need_content = _need_content()

    tried: set[str] = set()
    last_err: Exception | None = None

    while len(tried) < len(api_key_pool):
        remaining = [k for k in api_key_pool if k not in tried]
        api_key = _select_api_key(remaining)
        if api_key is None:
            break
        tried.add(api_key)
        try:
            results = await _do_deepseek_search(query, max_results, api_key, need_content)
            logger.info(t("log.ai.websearch_deepseek_search_query", query=query, p0=len(results)))
            return results
        except (DeepSeekSearchError, aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
            last_err = e
            tail = api_key[-4:] if len(api_key) >= 4 else "key"
            logger.warning(t("log.ai.websearch_deepseek_api_key_trying", p0=tail))
            continue

    msg = t("log.ai.websearch_deepseek_api_keys")
    if last_err:
        raise DeepSeekSearchError(f"{msg}: {last_err}") from last_err
    raise DeepSeekSearchError(msg)


def deepseek_search_configured() -> bool:
    """是否至少配置了一个 DeepSeek API Key。"""
    return bool(_get_api_key_pool())
