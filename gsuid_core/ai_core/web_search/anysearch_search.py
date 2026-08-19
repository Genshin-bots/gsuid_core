"""
AnySearch Web Search（POST https://api.anysearch.com/v1/search）

文档：https://www.anysearch.com/docs#quick-start
鉴权：Authorization: Bearer <api_key> 可选；无 Key 走匿名（按 IP 限流 + 每日免费额度）。
无效 / 过期 Key 返回 401/403，不会静默回落匿名。
"""

from __future__ import annotations

import json
import random
from typing import TypedDict

import aiohttp

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import anysearch_config

_SEARCH_URL = "https://api.anysearch.com/v1/search"
_CLIENT_HEADER = "gsuid-core/1.0"
_MAX_RESULTS_CAP = 100


class AnySearchError(RuntimeError):
    """AnySearch 搜索失败（额度/鉴权/网络等），供上层多源切换捕获。"""


class _SearchBody(TypedDict, total=False):
    query: str
    max_results: int
    zone: str
    language: str


def _get_api_key_pool() -> list[str]:
    api_key_data = anysearch_config.get_config("api_key").data
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
    n = _as_int(anysearch_config.get_config("timeout").data, 30)
    if n < 5:
        return 5
    if n > 120:
        return 120
    return n


def _clamp_max_results(n: int) -> int:
    if n < 1:
        return 1
    if n > _MAX_RESULTS_CAP:
        return _MAX_RESULTS_CAP
    return n


def _optional_enum(field: str, allowed: tuple[str, ...]) -> str:
    raw = anysearch_config.get_config(field).data
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if value in allowed:
        return value
    return ""


def _build_search_body(query: str, max_results: int) -> _SearchBody:
    body: _SearchBody = {"query": query, "max_results": max_results}
    zone = _optional_enum("zone", ("cn", "intl"))
    if zone:
        body["zone"] = zone
    language = _optional_enum("language", ("zh-CN", "en"))
    if language:
        body["language"] = language
    return body


def _first_str(item: dict[str, object], *keys: str) -> str:
    for key in keys:
        if key in item and item[key] is not None:
            return str(item[key])
    return ""


def _normalize_results(data: object, max_results: int) -> list[dict[str, str | float]]:
    """将 REST `{code, data.results[]}` 归一为 title/url/content/score。"""
    items: list[object]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        nested = data["data"] if "data" in data else None
        if isinstance(nested, dict) and "results" in nested and isinstance(nested["results"], list):
            items = nested["results"]
        elif "results" in data and isinstance(data["results"], list):
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
        content = _first_str(item, "content", "snippet")
        if not title and not url and not content:
            continue
        out.append({"title": title, "url": url, "content": content, "score": 0.0})
        if len(out) >= max_results:
            break
    return out


def _error_message(payload: object, fallback: str) -> str:
    if not isinstance(payload, dict):
        return fallback
    if "message" in payload and payload["message"] is not None:
        return str(payload["message"])
    return fallback


def _request_id(payload: object) -> str:
    if isinstance(payload, dict) and "request_id" in payload and payload["request_id"] is not None:
        return str(payload["request_id"])
    return ""


def _payload_code(payload: object) -> int | None:
    if not isinstance(payload, dict) or "code" not in payload:
        return None
    raw = payload["code"]
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


async def _do_anysearch_search(query: str, max_results: int, api_key: str) -> list[dict[str, str | float]]:
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "gsuid-core-anysearch/1.0",
        "X-Anysearch-Client": _CLIENT_HEADER,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    timeout = aiohttp.ClientTimeout(total=_timeout_sec())
    body = _build_search_body(query, max_results)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(_SEARCH_URL, json=body, headers=headers) as resp:
            text = await resp.text()
            payload: object
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError:
                payload = {"message": text[:300]}

            rid = _request_id(payload)
            rid_part = f" request_id={rid}" if rid else ""
            if resp.status == 401 or resp.status == 403:
                raise AnySearchError(
                    f"AnySearch auth HTTP {resp.status}{rid_part}: {_error_message(payload, text[:300])}"
                )
            if resp.status == 402:
                raise AnySearchError(f"AnySearch quota HTTP 402{rid_part}: {_error_message(payload, text[:300])}")
            if resp.status == 429:
                raise AnySearchError(f"AnySearch rate limit HTTP 429{rid_part}: {_error_message(payload, text[:300])}")
            if resp.status >= 400:
                raise AnySearchError(
                    f"AnySearch search HTTP {resp.status}{rid_part}: {_error_message(payload, text[:300])}"
                )

            code = _payload_code(payload)
            if code not in (None, 0, 200):
                raise AnySearchError(
                    f"AnySearch search error code={code}{rid_part}: {_error_message(payload, text[:200])}"
                )
            return _normalize_results(payload, max_results)


async def anysearch_search(
    query: str,
    max_results: int | None = None,
) -> list[dict[str, str | float]]:
    """
    使用 AnySearch REST API 进行 web 搜索。

    Key 池非空时轮询重试；全空则匿名调用。无效 Key 不会改走匿名。
    """
    if max_results is None:
        max_results = _as_int(anysearch_config.get_config("max_results").data, 10)
    max_results = _clamp_max_results(max_results)

    api_key_pool = _get_api_key_pool()
    keys_to_try = api_key_pool if api_key_pool else [""]
    tried: set[str] = set()
    last_err: Exception | None = None

    while len(tried) < len(keys_to_try):
        remaining = [k for k in keys_to_try if k not in tried]
        api_key = _select_api_key(remaining)
        if api_key is None:
            break
        tried.add(api_key)
        try:
            results = await _do_anysearch_search(query, max_results, api_key)
            logger.info(t("log.ai.websearch_anysearch_search_query", query=query, p0=len(results)))
            return results
        except (AnySearchError, aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
            last_err = e
            tail = api_key[-4:] if api_key else "anon"
            logger.warning(t("log.ai.websearch_anysearch_api_key_trying", p0=tail))
            continue

    msg = t("log.ai.websearch_anysearch_api_keys")
    if last_err:
        raise AnySearchError(f"{msg}: {last_err}") from last_err
    raise AnySearchError(msg)


def anysearch_search_configured() -> bool:
    """AnySearch 可匿名调用，选作主用/备用即视为已配置。"""
    return True
