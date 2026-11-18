"""
Keenable Web Search & Fetch：
- Search：POST https://api.keenable.ai/v1/search 与 /v1/search/public
- Fetch：GET https://api.keenable.ai/v1/fetch 与 /v1/fetch/public

文档：https://docs.keenable.ai/
鉴权：仅从 keenable_config 读取 api_key（永不读取环境变量）。
未填 Key 时走公开端点 /public 并携带 X-Keenable-Title；
填写 Key 后走正式端点并携带 X-API-Key，无效 Key 不会回落匿名。
默认不进入搜索/抓取链，只有将主用或备用源设为 Keenable 时才会调用。
"""

from __future__ import annotations

import json
import random
from typing import TypedDict

import aiohttp

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import keenable_config

from .date_utils import normalize_published_date

PUBLIC_SEARCH_URL = "https://api.keenable.ai/v1/search/public"
KEYED_SEARCH_URL = "https://api.keenable.ai/v1/search"
PUBLIC_FETCH_URL = "https://api.keenable.ai/v1/fetch/public"
KEYED_FETCH_URL = "https://api.keenable.ai/v1/fetch"
APP_TITLE = "gsuid_core"
_MAX_RESULTS_CAP = 50


class KeenableSearchError(RuntimeError):
    """Keenable 搜索失败（额度/鉴权/网络等），供上层多源切换捕获。"""


class KeenableRequestError(KeenableSearchError):
    """Keenable 请求参数错误（400/422），换 Key 不会成功，直接终止轮询。"""


class _SearchBody(TypedDict):
    query: str
    max_results: int
    mode: str


def _get_api_key_pool() -> list[str]:
    api_key_data = keenable_config.get_config("api_key").data
    if isinstance(api_key_data, list):
        return [k.strip() for k in api_key_data if isinstance(k, str) and k.strip()]
    if isinstance(api_key_data, str) and api_key_data.strip():
        return [api_key_data.strip()]
    return []


def _select_api_key(api_key_pool: list[str]) -> str | None:
    if not api_key_pool:
        return None
    return random.choice(api_key_pool)


def _as_int(raw: object, default: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return raw


def _clamp_max_results(n: int) -> int:
    if n < 1:
        return 1
    if n > _MAX_RESULTS_CAP:
        return _MAX_RESULTS_CAP
    return n


def _timeout_sec() -> int:
    n = _as_int(keenable_config.get_config("timeout").data, 20)
    if n < 5:
        return 5
    if n > 120:
        return 120
    return n


def _search_mode() -> str:
    raw = keenable_config.get_config("mode").data
    if isinstance(raw, str):
        mode = raw.strip().lower()
        if mode in ("pro", "realtime"):
            return mode
    return "pro"


def _first_str(item: dict[str, object], *keys: str) -> str:
    for key in keys:
        if key in item and item[key] is not None:
            val = str(item[key]).strip()
            if val:
                return val
    return ""


def _parse_score(item: dict[str, object]) -> float:
    if "score" not in item:
        return 0.0
    raw = item["score"]
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            return 0.0
    return 0.0


def _normalize_items(payload: object, max_results: int) -> list[dict[str, str | float]]:
    if max_results <= 0 or not isinstance(payload, dict) or "results" not in payload:
        return []
    items = payload["results"]
    if not isinstance(items, list):
        return []
    out: list[dict[str, str | float]] = []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        item: dict[str, object] = {k: v for k, v in raw_item.items() if isinstance(k, str)}
        url = _first_str(item, "url", "link")
        if not url:
            continue
        title = _first_str(item, "title")
        content = _first_str(item, "snippet", "content", "description")
        published = normalize_published_date(_first_str(item, "published_at", "published_date", "published"))
        out.append(
            {
                "title": title,
                "url": url,
                "content": content,
                "published_date": published,
                "score": _parse_score(item),
            }
        )
        if len(out) >= max_results:
            break
    return out


async def _read_json_payload(resp: aiohttp.ClientResponse) -> tuple[object, str]:
    text = await resp.text()
    try:
        return (json.loads(text) if text else {}), text
    except json.JSONDecodeError:
        return {"message": text[:300]}, text


def _error_detail(payload: object, fallback: str) -> str:
    if not isinstance(payload, dict):
        return f": {fallback}" if fallback else ""
    code = payload["code"] if "code" in payload and payload["code"] is not None else ""
    rid = payload["request_id"] if "request_id" in payload and payload["request_id"] is not None else ""
    msg = ""
    for key in ("message", "error"):
        if key in payload and payload[key] is not None and str(payload[key]).strip():
            msg = str(payload[key]).strip()
            break
    parts: list[str] = []
    if code:
        parts.append(f"code={code}")
    if rid:
        parts.append(f"request_id={rid}")
    prefix = f" ({', '.join(parts)})" if parts else ""
    body = msg or fallback
    if not body:
        return prefix
    return f"{prefix}: {body}"


def _raise_for_status(status: int, payload: object, text: str) -> None:
    if status < 400:
        return
    detail = _error_detail(payload, text[:300])
    if status in (400, 422):
        raise KeenableRequestError(f"Keenable bad request HTTP {status}{detail}")
    if status in (401, 403):
        raise KeenableSearchError(f"Keenable auth HTTP {status}{detail}")
    if status == 402:
        raise KeenableSearchError(f"Keenable credits HTTP 402{detail}")
    if status == 429:
        raise KeenableSearchError(f"Keenable rate limit HTTP 429{detail}")
    raise KeenableSearchError(f"Keenable search HTTP {status}{detail}")


async def _do_keenable_search(
    query: str,
    max_results: int,
    api_key: str,
) -> list[dict[str, str | float]]:
    url = KEYED_SEARCH_URL if api_key else PUBLIC_SEARCH_URL
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Keenable-Title": APP_TITLE,
        "User-Agent": "gsuid-core-keenable-search/1.0",
    }
    if api_key:
        headers["X-API-Key"] = api_key
    body: _SearchBody = {
        "query": query,
        "max_results": max_results,
        "mode": _search_mode(),
    }
    timeout = aiohttp.ClientTimeout(total=_timeout_sec())
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=body, headers=headers) as resp:
            payload, text = await _read_json_payload(resp)
            _raise_for_status(resp.status, payload, text)
            if not isinstance(payload, dict):
                raise KeenableSearchError(f"Keenable returned unexpected JSON: {text[:300]}")
            return _normalize_items(payload, max_results)


async def keenable_search(
    query: str,
    max_results: int | None = None,
) -> list[dict[str, str | float]]:
    """使用 Keenable Search。仅读取 keenable_config，无 Key 时走公开端点。"""
    cleaned_query = query.strip()
    if not cleaned_query:
        raise KeenableSearchError("Keenable search query must not be empty")

    if max_results is None:
        max_results = _as_int(keenable_config.get_config("max_results").data, 10)
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
            results = await _do_keenable_search(cleaned_query, max_results, api_key)
            logger.info(t("log.ai.websearch_keenable_search_query", query=cleaned_query, p0=len(results)))
            return results
        except KeenableRequestError:
            raise
        except (KeenableSearchError, aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
            last_err = e
            if api_key and len(tried) < len(keys_to_try):
                tail = api_key[-4:] if len(api_key) >= 4 else "key"
                logger.warning(t("log.ai.websearch_keenable_api_key_trying", p0=tail))
            continue

    msg = t("log.ai.websearch_keenable_api_keys")
    if last_err:
        raise KeenableSearchError(f"{msg}: {last_err}") from last_err
    raise KeenableSearchError(msg)


def keenable_search_configured() -> bool:
    """Keenable 可 keyless 调用，选作主用/备用即视为已配置。"""
    return True


def _fetch_live() -> bool:
    raw = keenable_config.get_config("fetch_live").data
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return False


def _extract_fetch_markdown(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    raw_content = payload["content"] if "content" in payload else None
    if not isinstance(raw_content, str):
        return ""
    content = raw_content.strip()
    if not content:
        return ""
    raw_title = payload["title"] if "title" in payload else None
    if isinstance(raw_title, str) and raw_title.strip() and not content.startswith("#"):
        return f"# {raw_title.strip()}\n\n{content}"
    return content


async def _do_keenable_fetch(
    url: str,
    max_chars: int,
    api_key: str,
    *,
    live: bool,
) -> str:
    endpoint = KEYED_FETCH_URL if api_key else PUBLIC_FETCH_URL
    headers: dict[str, str] = {
        "Accept": "application/json",
        "X-Keenable-Title": APP_TITLE,
        "User-Agent": "gsuid-core-keenable-fetch/1.0",
    }
    if api_key:
        headers["X-API-Key"] = api_key
    params: dict[str, str | int] = {
        "url": url,
        "max_chars": max(1, max_chars),
        "live": "true" if live else "false",
    }
    timeout = aiohttp.ClientTimeout(total=_timeout_sec())
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(endpoint, params=params, headers=headers) as resp:
            payload, text = await _read_json_payload(resp)
            _raise_for_status(resp.status, payload, text)
            if not isinstance(payload, dict):
                raise KeenableSearchError(f"Keenable returned unexpected JSON: {text[:300]}")
            return _extract_fetch_markdown(payload)


async def keenable_fetch_markdown(url: str, max_chars: int) -> str:
    """使用 Keenable Fetch API 提取网页 Markdown（无 Key 走 /v1/fetch/public）。"""
    api_key_pool = _get_api_key_pool()
    keys_to_try = api_key_pool if api_key_pool else [""]
    auth_mode = "with_api_key" if api_key_pool else "anonymous"
    endpoint = KEYED_FETCH_URL if api_key_pool else PUBLIC_FETCH_URL
    live = _fetch_live()

    logger.info(
        t(
            "log.ai.webfetch_start",
            provider="Keenable",
            url=url,
            detail=f"endpoint={endpoint} auth={auth_mode}",
        )
    )

    tried: set[str] = set()
    last_err: Exception | None = None
    while len(tried) < len(keys_to_try):
        remaining = [k for k in keys_to_try if k not in tried]
        api_key = _select_api_key(remaining)
        if api_key is None:
            break
        tried.add(api_key)
        try:
            result = await _do_keenable_fetch(url, max_chars, api_key, live=live)
            if not result:
                raise ValueError(t("抓取结果为空，切换下一源"))
            if len(result) > max_chars:
                result = result[:max_chars] + "\n\n...(内容已截断)"
            logger.info(t("log.ai.webfetch_keenable_ok", url=url, p0=len(result)))
            return result
        except KeenableRequestError as e:
            raise ValueError(t("Keenable 抓取失败: {e}", e=e)) from e
        except ValueError:
            raise
        except (KeenableSearchError, aiohttp.ClientError, TimeoutError, OSError) as e:
            last_err = e
            if api_key and len(tried) < len(keys_to_try):
                tail = api_key[-4:] if len(api_key) >= 4 else "key"
                logger.warning(t("log.ai.webfetch_keenable_try_fail", p0=tail, url=url, e=e))
            continue

    if last_err is not None:
        raise ValueError(t("Keenable 抓取失败: {e}", e=last_err)) from last_err
    raise ValueError(t("Keenable 抓取失败: empty response"))
