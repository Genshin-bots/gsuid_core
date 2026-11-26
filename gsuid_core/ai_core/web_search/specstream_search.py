"""
SpecStream Web Search 模块

通过 httpx2 直调 SpecStream Search API（GET https://api.specstream.ai/v1/web-search），
Bearer 鉴权，仅读 ``specstream_config``；支持 api_key 池轮询。
"""

from __future__ import annotations

import random

import httpx2

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import specstream_config

from .date_utils import normalize_published_date

SEARCH_URL = "https://api.specstream.ai/v1/web-search"
_TIMEOUT_SEC = 30.0
_MAX_NUM = 20


def _get_api_key_pool() -> list[str]:
    api_key_data = specstream_config.get_config("api_key").data
    if isinstance(api_key_data, list):
        return [k.strip() for k in api_key_data if isinstance(k, str) and k.strip()]
    if isinstance(api_key_data, str) and api_key_data.strip():
        return [api_key_data.strip()]
    return []


def specstream_search_configured() -> bool:
    return bool(_get_api_key_pool())


def _clamp_num(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return 10
    try:
        n = int(raw)
    except ValueError:
        return 10
    return max(1, min(n, _MAX_NUM))


def _speed() -> str:
    raw = specstream_config.get_config("speed").data
    return "fast" if str(raw or "").strip().lower() == "fast" else "default"


def _str_field(item: dict[str, object], key: str) -> str:
    if key not in item or item[key] is None:
        return ""
    return str(item[key])


def _data_of(payload: object) -> dict[str, object]:
    """取出 ``{"data": {...}}`` 中的对象；结构不符时抛 ValueError 以触发换源。"""
    if not isinstance(payload, dict) or "data" not in payload or not isinstance(payload["data"], dict):
        raise ValueError("SpecStream 响应缺少 data 对象")
    out: dict[str, object] = {}
    for key, val in payload["data"].items():
        if isinstance(key, str):
            out[key] = val
    return out


def _parse_results(data: dict[str, object], max_results: int) -> list[dict[str, str | float]]:
    results: list[dict[str, str | float]] = []
    raw_items = data["sources"] if "sources" in data else None
    if not isinstance(raw_items, list):
        return results
    for item in raw_items:
        if len(results) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        url = _str_field(item, "url").strip()
        if not url.startswith(("http://", "https://")):
            continue
        published = normalize_published_date(item["published"]) if "published" in item else ""
        results.append(
            {
                "title": _str_field(item, "title"),
                "url": url,
                "content": _str_field(item, "snippet"),
                "published_date": published or "",
                "score": 0.0,
            }
        )
    return results


async def _get_search(query: str, num: int, api_key: str) -> dict[str, object]:
    params: dict[str, str | int] = {"q": query, "num": num}
    if _speed() == "fast":
        params["speed"] = "fast"
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(_TIMEOUT_SEC), follow_redirects=False) as client:
        resp = await client.get(SEARCH_URL, params=params, headers=headers)
        resp.raise_for_status()
        payload: object = resp.json()
    return _data_of(payload)


async def specstream_search(
    query: str,
    max_results: int | None = None,
) -> list[dict[str, str | float]]:
    """使用 SpecStream Search API 搜索，返回结果列表。"""
    api_key_pool = _get_api_key_pool()
    if not api_key_pool:
        logger.warning(t("log.ai.websearch_specstream_api_key_skip"))
        raise RuntimeError(t("log.ai.websearch_specstream_api_key_skip"))

    num = _clamp_num(max_results if max_results is not None else specstream_config.get_config("max_results").data)
    remaining = list(api_key_pool)
    last_err: Exception | None = None
    while remaining:
        api_key = random.choice(remaining)
        remaining.remove(api_key)
        try:
            data = await _get_search(query, num, api_key)
        except (httpx2.HTTPError, OSError, ValueError, TypeError) as e:
            logger.warning(t("log.ai.websearch_specstream_api_key_trying", p0=api_key[-4:]))
            last_err = e
            continue
        results = _parse_results(data, num)
        logger.info(t("log.ai.websearch_specstream_search_query", query=query, p0=len(results)))
        return results

    logger.error(t("log.ai.websearch_specstream_api_keys"))
    raise RuntimeError(t("log.ai.websearch_specstream_api_keys") + (f": {last_err}" if last_err else ""))
