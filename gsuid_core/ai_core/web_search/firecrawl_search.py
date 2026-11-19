"""
Firecrawl Web Search（httpx2 直调 POST https://api.firecrawl.dev/v2/search）

不加载第三方 firecrawl-py SDK。
鉴权：api_key 可选；无 Key 走 keyless 免费档（按 IP 限流）。无效 Key 不会回落匿名。
"""

from __future__ import annotations

import random

import httpx2

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import firecrawl_config

_FIRECRAWL_SEARCH_URL = "https://api.firecrawl.dev/v2/search"
_MAX_RESULTS_CAP = 100
_IMG_CAP = 6


class FirecrawlSearchError(RuntimeError):
    """Firecrawl 搜索失败（额度/鉴权/网络等），供上层多源切换捕获。"""


def _get_api_key_pool() -> list[str]:
    api_key_data = firecrawl_config.get_config("api_key").data
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
    n = _as_int(firecrawl_config.get_config("timeout").data, 30)
    if n < 5:
        return 5
    if n > 120:
        return 120
    return n


def _timeout_ms() -> int:
    return _timeout_sec() * 1000


def _clamp_max_results(n: int) -> int:
    if n < 1:
        return 1
    if n > _MAX_RESULTS_CAP:
        return _MAX_RESULTS_CAP
    return n


def _first_str(item: dict[str, object], *keys: str) -> str:
    for key in keys:
        if key in item and item[key] is not None:
            return str(item[key])
    return ""


def _as_str_map(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        return {}
    out: dict[str, object] = {}
    for key, value in item.items():
        if isinstance(key, str):
            out[key] = value
    return out


def _published_from_mapping(item: dict[str, object]) -> str:
    direct = _first_str(item, "published_time", "publishedTime").strip()
    if direct:
        return direct
    raw_meta = item["metadata"] if "metadata" in item else None
    meta = _as_str_map(raw_meta)
    if not meta:
        return ""
    published = _first_str(meta, "published_time", "publishedTime").strip()
    if published:
        return published
    return _first_str(meta, "dc_date", "dcDate").strip()


def _hit_from_mapping(item: dict[str, object]) -> dict[str, str | float] | None:
    meta = _as_str_map(item["metadata"]) if "metadata" in item else {}
    title = _first_str(item, "title") or _first_str(meta, "title", "ogTitle", "og_title")
    url = _first_str(item, "url", "source_url", "sourceURL") or _first_str(meta, "url", "ogUrl", "og_url")
    content = _first_str(item, "markdown", "description", "content", "snippet", "summary") or _first_str(
        meta, "description", "ogDescription", "og_description"
    )
    published = _published_from_mapping(item)
    if not title and not url and not content:
        return None
    return {"title": title, "url": url, "content": content, "published_date": published, "score": 0.0}


def _normalize_results(payload: object, max_results: int) -> list[dict[str, str | float]]:
    """将 /v2/search 的 data（web / images）归一为 title/url/content/score。"""
    mapped_payload = _as_str_map(payload)
    web_raw = mapped_payload["web"] if "web" in mapped_payload else None
    images_raw = mapped_payload["images"] if "images" in mapped_payload else None

    out: list[dict[str, str | float]] = []
    if isinstance(web_raw, list):
        for item in web_raw:
            hit = _hit_from_mapping(_as_str_map(item))
            if hit is None:
                continue
            out.append(hit)
            if len(out) >= max_results:
                break

    img_n = 0
    if isinstance(images_raw, list):
        for item in images_raw:
            if img_n >= _IMG_CAP:
                break
            mapped = _as_str_map(item)
            img_url = _first_str(mapped, "imageUrl", "image_url", "url")
            if not img_url.startswith(("http://", "https://")):
                continue
            title = _first_str(mapped, "title") or "(配图)"
            out.append(
                {
                    "title": title,
                    "url": img_url,
                    "content": "",
                    "published_date": "",
                    "score": 0.0,
                    "image_url": img_url,
                    "kind": "image",
                }
            )
            img_n += 1
    return out


async def _do_firecrawl_search(query: str, max_results: int, api_key: str) -> list[dict[str, str | float]]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, object] = {
        "query": query,
        "limit": max_results,
        "timeout": _timeout_ms(),
    }
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(float(_timeout_sec()))) as client:
        resp = await client.post(_FIRECRAWL_SEARCH_URL, json=payload, headers=headers)
        if resp.status_code != 200:
            raise FirecrawlSearchError(f"Firecrawl HTTP {resp.status_code}: {resp.text[:200]}")
        body = _as_str_map(resp.json())
    if not body or "success" not in body or body["success"] is not True:
        err_msg = _first_str(body, "error", "message") or "Firecrawl search failed"
        raise FirecrawlSearchError(err_msg)
    data = body["data"] if "data" in body else {}
    return _normalize_results(data, max_results)


async def firecrawl_search(
    query: str,
    max_results: int | None = None,
) -> list[dict[str, str | float]]:
    """使用 Firecrawl REST API 进行 web 搜索；Key 池非空时轮询重试，全空走 keyless 免费档。"""
    if max_results is None:
        max_results = _as_int(firecrawl_config.get_config("max_results").data, 10)
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
            results = await _do_firecrawl_search(query, max_results, api_key)
            logger.info(t("log.ai.websearch_firecrawl_search_query", query=query, p0=len(results)))
            return results
        except (
            FirecrawlSearchError,
            httpx2.HTTPError,
            TimeoutError,
            OSError,
            ValueError,
            TypeError,
        ) as e:
            last_err = e
            tail = api_key[-4:] if api_key else "anon"
            logger.warning(t("log.ai.websearch_firecrawl_api_key_trying", p0=tail))
            continue

    msg = t("log.ai.websearch_firecrawl_api_keys")
    if last_err:
        raise FirecrawlSearchError(f"{msg}: {last_err}") from last_err
    raise FirecrawlSearchError(msg)


def firecrawl_search_configured() -> bool:
    """Firecrawl 可 keyless 调用，选作主用/备用即视为已配置。"""
    return True
