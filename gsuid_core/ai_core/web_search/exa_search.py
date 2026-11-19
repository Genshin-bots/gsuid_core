"""Exa 网页搜索与按 URL 抓取（httpx2 直调 REST API，不导入 exa_py SDK）。

api_key 可以配多把，失败后换一把重试。
单次请求 30 秒超时，短于工具外层的 100 秒以便及时换 Key。
"""

from __future__ import annotations

import random
import asyncio
from typing import Literal, TypeVar, NoReturn
from collections.abc import Callable, Awaitable

import httpx2

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import exa_config

from .date_utils import normalize_published_date

_EXA_BASE_URL = "https://api.exa.ai"
_EXA_TIMEOUT_SEC = 30
_EXA_TEXT_MAX = 1_000_000
_Kind = Literal["search", "fetch"]
_T = TypeVar("_T")

_RETRYABLE: tuple[type[Exception], ...] = (
    RuntimeError,
    OSError,
    ValueError,
    TypeError,
    TimeoutError,
    httpx2.HTTPError,
)


def _get_api_key_pool() -> list[str]:
    api_key_data = exa_config.get_config("api_key").data

    if isinstance(api_key_data, list):
        return [k for k in api_key_data if isinstance(k, str) and k]
    if isinstance(api_key_data, str) and api_key_data:
        return [api_key_data]
    return []


def _select_api_key(api_key_pool: list[str]) -> str | None:
    if not api_key_pool:
        return None
    return random.choice(api_key_pool)


def _log_key_failure(kind: _Kind, api_key: str, url: str, err: Exception) -> None:
    tail = api_key[-4:]
    if kind == "search":
        logger.warning(t("log.ai.websearch_exa_api_key_trying", p0=tail))
        return
    logger.warning(t("log.ai.webfetch_exa_try_fail", url=url, e=err, p0=tail))


def _raise_no_key(kind: _Kind) -> NoReturn:
    if kind == "search":
        logger.warning(t("log.ai.websearch_exa_api_key_configured"))
        raise RuntimeError(t("log.ai.websearch_exa_api_key_configured"))
    logger.warning(t("log.ai.webfetch_exa_no_key"))
    raise ValueError(t("log.ai.webfetch_exa_no_key"))


def _raise_all_failed(kind: _Kind, last_err: Exception | None) -> NoReturn:
    detail = f": {last_err}" if last_err else ""
    if kind == "search":
        logger.error(t("log.ai.websearch_exa_api_keys"))
        raise RuntimeError(t("log.ai.websearch_exa_api_keys") + detail)
    logger.error(t("log.ai.webfetch_exa_api_keys"))
    raise ValueError(t("log.ai.webfetch_exa_api_keys") + detail)


def _as_dict(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, object] = {}
    for k, v in raw.items():
        if isinstance(k, str):
            out[k] = v
    return out


async def _post_exa(path: str, api_key: str, payload: dict[str, object]) -> dict[str, object]:
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }
    async with httpx2.AsyncClient(
        base_url=_EXA_BASE_URL,
        timeout=httpx2.Timeout(float(_EXA_TIMEOUT_SEC)),
    ) as client:
        resp = await client.post(path, json=payload, headers=headers)
        resp.raise_for_status()
        parsed = _as_dict(resp.json())
    if not parsed:
        raise ValueError("Exa 响应非 JSON 对象")
    return parsed


async def _with_keys(
    kind: _Kind,
    pool: list[str],
    call: Callable[[str], Awaitable[_T]],
    *,
    url: str = "",
) -> _T:
    tried: set[str] = set()
    last_err: Exception | None = None
    while len(tried) < len(pool):
        api_key = _select_api_key([k for k in pool if k not in tried])
        if not api_key:
            break
        tried.add(api_key)
        try:
            return await asyncio.wait_for(call(api_key), _EXA_TIMEOUT_SEC)
        except _RETRYABLE as e:
            _log_key_failure(kind, api_key, url, e)
            last_err = e
    _raise_all_failed(kind, last_err)


def _extract_hit_content(item: dict[str, object]) -> str:
    raw_hl = item["highlights"] if "highlights" in item else None
    if isinstance(raw_hl, list):
        lines = [piece.strip() for piece in raw_hl if isinstance(piece, str) and piece.strip()]
        if lines:
            return "\n".join(lines)
    raw_text = item["text"] if "text" in item else None
    if isinstance(raw_text, str) and raw_text.strip():
        return raw_text.strip()
    return ""


def _published_date(item: dict[str, object]) -> str:
    for key in ("publishedDate", "published_date"):
        if key in item and isinstance(item[key], str):
            return normalize_published_date(item[key])
    return ""


async def _do_exa_search(
    api_key: str,
    query: str,
    max_results: int,
) -> list[dict[str, str | float]]:
    payload: dict[str, object] = {
        "query": query,
        "numResults": max_results,
        "contents": {
            "highlights": True,
            "text": {"maxCharacters": 1000},
        },
    }
    response = await _post_exa("/search", api_key, payload)
    raw_results = response["results"] if "results" in response else None
    results: list[dict[str, str | float]] = []
    if not isinstance(raw_results, list):
        return results
    for raw_item in raw_results:
        item = _as_dict(raw_item)
        if not item:
            continue
        title = str(item["title"]) if "title" in item and item["title"] is not None else ""
        url = str(item["url"]) if "url" in item and item["url"] is not None else ""
        raw_score = item["score"] if "score" in item else None
        score = float(raw_score) if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool) else 0.0
        results.append(
            {
                "title": title,
                "url": url,
                "content": _extract_hit_content(item),
                "published_date": _published_date(item),
                "score": score,
            }
        )
    return results


async def exa_search(
    query: str,
    max_results: int | None = None,
) -> list[dict[str, str | float]]:
    """使用 Exa API 进行 web 搜索，支持 api_key 池轮询重试。"""
    api_key_pool = _get_api_key_pool()
    if not api_key_pool:
        _raise_no_key("search")

    if max_results is None:
        max_results = int(exa_config.get_config("max_results").data or "10")

    async def _call(api_key: str) -> list[dict[str, str | float]]:
        return await _do_exa_search(api_key, query, max_results)

    return await _with_keys("search", api_key_pool, _call)


def _text_limit(max_chars: int) -> int:
    if max_chars < 1:
        return 1
    if max_chars > _EXA_TEXT_MAX:
        return _EXA_TEXT_MAX
    return max_chars


def _text_from_contents(response: dict[str, object]) -> str:
    raw_statuses = response["statuses"] if "statuses" in response else None
    if isinstance(raw_statuses, list) and raw_statuses:
        saw_success = False
        for item in raw_statuses:
            mapped = _as_dict(item)
            if "status" in mapped and mapped["status"] == "success":
                saw_success = True
                break
        if not saw_success:
            return ""
    raw_results = response["results"] if "results" in response else None
    if not isinstance(raw_results, list) or not raw_results:
        return ""
    first = _as_dict(raw_results[0])
    raw_text = first["text"] if "text" in first else None
    if not isinstance(raw_text, str):
        return ""
    return raw_text.strip()


async def exa_fetch_markdown(url: str, max_chars: int) -> str:
    """用 Exa Contents 读取指定 URL 的正文文本；空正文抛 ValueError 供抓取链换源。"""
    api_key_pool = _get_api_key_pool()
    if not api_key_pool:
        _raise_no_key("fetch")

    limit = _text_limit(max_chars)
    logger.info(
        t(
            "log.ai.webfetch_start",
            provider="Exa",
            url=url,
            detail=f"endpoint=https://api.exa.ai/contents timeout={_EXA_TIMEOUT_SEC}s",
        )
    )

    async def _call(api_key: str) -> str:
        payload: dict[str, object] = {
            "urls": [url],
            "text": {"maxCharacters": limit},
        }
        response = await _post_exa("/contents", api_key, payload)
        return _text_from_contents(response)

    text = await _with_keys("fetch", api_key_pool, _call, url=url)
    if not text:
        raise ValueError(t("抓取结果为空，切换下一源"))
    if len(text) > limit:
        text = text[:limit] + "\n\n...(内容已截断)"
    logger.info(t("log.ai.webfetch_exa_ok", url=url, p0=len(text)))
    return text
