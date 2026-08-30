"""
Firecrawl Web Search（官方 SDK ``AsyncFirecrawl.search`` → POST /v2/search）

文档：https://docs.firecrawl.dev/sdks/python
鉴权：api_key 可选；无 Key 走 keyless 免费档（按 IP 限流）。无效 Key 不会回落匿名。
"""

from __future__ import annotations

import random
from typing import Protocol, runtime_checkable

import httpx
from firecrawl import AsyncFirecrawl
from firecrawl.v2.types import Document, SearchData, SearchResultWeb, SearchResultImages
from firecrawl.v2.utils.error_handler import FirecrawlError

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import firecrawl_config

_MAX_RESULTS_CAP = 100
_IMG_CAP = 6


class FirecrawlSearchError(RuntimeError):
    """Firecrawl 搜索失败（额度/鉴权/网络等），供上层多源切换捕获。"""


@runtime_checkable
class _SearchClient(Protocol):
    async def search(self, query: str, limit: int, timeout: int) -> object: ...


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


def _new_client(api_key: str) -> object:
    timeout_sec = float(_timeout_sec())
    if api_key:
        return AsyncFirecrawl(api_key=api_key, timeout=timeout_sec)
    return AsyncFirecrawl(timeout=timeout_sec)


def _hit_from_web(item: SearchResultWeb) -> dict[str, str | float] | None:
    title = item.title if item.title else ""
    url = item.url if item.url else ""
    content = item.description if item.description else ""
    if not title and not url and not content:
        return None
    return {"title": title, "url": url, "content": content, "score": 0.0}


def _hit_from_document(item: Document) -> dict[str, str | float] | None:
    title = ""
    url = ""
    content = ""
    meta = item.metadata
    if meta is not None:
        if meta.title:
            title = meta.title
        elif meta.og_title:
            title = meta.og_title
        if meta.url:
            url = meta.url
        elif meta.og_url:
            url = meta.og_url
        if meta.description:
            content = meta.description
        elif meta.og_description:
            content = meta.og_description
    if item.markdown:
        content = item.markdown
    elif item.summary and not content:
        content = item.summary
    if not title and not url and not content:
        return None
    return {"title": title, "url": url, "content": content, "score": 0.0}


def _as_str_map(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        return {}
    out: dict[str, object] = {}
    for key, value in item.items():
        if isinstance(key, str):
            out[key] = value
    return out


def _hit_from_mapping(item: dict[str, object]) -> dict[str, str | float] | None:
    title = _first_str(item, "title")
    url = _first_str(item, "url", "source_url")
    content = _first_str(item, "description", "markdown", "content", "snippet")
    if not title and not url and not content:
        return None
    return {"title": title, "url": url, "content": content, "score": 0.0}


def _hit_from_image(item: SearchResultImages) -> dict[str, str | float] | None:
    img = item.image_url if item.image_url else ""
    page = item.url if item.url else ""
    url = img if img else page
    if not url.startswith(("http://", "https://")):
        return None
    title = item.title if item.title else "(配图)"
    return {
        "title": title,
        "url": url,
        "content": "",
        "score": 0.0,
        "image_url": url,
        "kind": "image",
    }


def _web_items(payload: object) -> list[object]:
    if isinstance(payload, SearchData):
        web = payload.web
        return list(web) if isinstance(web, list) else []
    if isinstance(payload, dict) and "web" in payload:
        raw = payload["web"]
        return raw if isinstance(raw, list) else []
    return []


def _image_items(payload: object) -> list[object]:
    if isinstance(payload, SearchData):
        images = payload.images
        return list(images) if isinstance(images, list) else []
    if isinstance(payload, dict) and "images" in payload:
        raw = payload["images"]
        return raw if isinstance(raw, list) else []
    return []


def _normalize_results(payload: object, max_results: int) -> list[dict[str, str | float]]:
    """将 SDK ``SearchData.web`` / dict ``web`` 归一为 title/url/content/score。"""
    out: list[dict[str, str | float]] = []
    for item in _web_items(payload):
        hit: dict[str, str | float] | None
        if isinstance(item, SearchResultWeb):
            hit = _hit_from_web(item)
        elif isinstance(item, Document):
            hit = _hit_from_document(item)
        elif isinstance(item, dict):
            hit = _hit_from_mapping(_as_str_map(item))
        else:
            continue
        if hit is None:
            continue
        out.append(hit)
        if len(out) >= max_results:
            break

    img_n = 0
    for item in _image_items(payload):
        if img_n >= _IMG_CAP:
            break
        img_hit: dict[str, str | float] | None
        if isinstance(item, SearchResultImages):
            img_hit = _hit_from_image(item)
        elif isinstance(item, dict):
            mapped = _as_str_map(item)
            img_url = _first_str(mapped, "image_url", "url")
            if not img_url.startswith(("http://", "https://")):
                continue
            title = _first_str(mapped, "title") or "(配图)"
            img_hit = {
                "title": title,
                "url": img_url,
                "content": "",
                "score": 0.0,
                "image_url": img_url,
                "kind": "image",
            }
        else:
            continue
        if img_hit is None:
            continue
        out.append(img_hit)
        img_n += 1
    return out


async def _do_firecrawl_search(query: str, max_results: int, api_key: str) -> list[dict[str, str | float]]:
    client = _new_client(api_key)
    if not isinstance(client, _SearchClient):
        raise FirecrawlSearchError("Firecrawl client missing search()")
    raw: object = await client.search(query, limit=max_results, timeout=_timeout_ms())
    return _normalize_results(raw, max_results)


async def firecrawl_search(
    query: str,
    max_results: int | None = None,
) -> list[dict[str, str | float]]:
    """
    使用 Firecrawl 官方 SDK 进行 web 搜索。

    Key 池非空时轮询重试；全空则 keyless 免费档。无效 Key 不会改走匿名。
    """
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
            FirecrawlError,
            httpx.HTTPError,
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
