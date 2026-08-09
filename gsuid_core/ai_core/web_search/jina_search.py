"""
Jina Web Search（s.jina.ai）

文档：https://jina.ai/reader / https://s.jina.ai
请求：GET/POST https://s.jina.ai/?q=...
鉴权：Authorization: Bearer <api_key>（必填；无 Key 返回 401）
"""

from __future__ import annotations

import json
import random
from typing import Any, Optional
from urllib.parse import quote

import aiohttp

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import jina_config


class JinaSearchError(RuntimeError):
    """Jina 搜索失败（额度/鉴权/网络等），供上层多源切换捕获。"""


def _get_api_key_pool() -> list[str]:
    api_key_data = jina_config.get_config("api_key").data
    if isinstance(api_key_data, list):
        return [k for k in api_key_data if k]
    if isinstance(api_key_data, str) and api_key_data:
        return [api_key_data]
    return []


def _select_api_key(api_key_pool: list[str]) -> Optional[str]:
    if not api_key_pool:
        return None
    return random.choice(api_key_pool)


def _timeout_sec() -> int:
    try:
        return max(5, min(120, int(jina_config.get_config("timeout").data or 30)))
    except (TypeError, ValueError):
        return 30


def _search_base_url() -> str:
    base = str(jina_config.get_config("search_base_url").data or "https://s.jina.ai").strip()
    return base.rstrip("/") or "https://s.jina.ai"


def _first_str(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key in item and item[key] is not None:
            return str(item[key])
    return ""


def _normalize_items(data: Any, max_results: int) -> list[dict]:
    """将 Jina 多种返回结构归一为 title/url/content/score。"""
    items: list[Any]
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        nested = data["data"] if "data" in data else None
        if isinstance(nested, list):
            items = nested
        elif isinstance(nested, dict) and "results" in nested and isinstance(nested["results"], list):
            items = nested["results"]
        elif "results" in data and isinstance(data["results"], list):
            items = data["results"]
        else:
            items = [data]
    else:
        items = []

    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _first_str(item, "title", "name")
        url = _first_str(item, "url", "link", "href")
        content = _first_str(item, "content", "description", "snippet", "text")
        score_raw = item["score"] if "score" in item else 0.0
        try:
            score = float(score_raw) if score_raw is not None else 0.0
        except (TypeError, ValueError):
            score = 0.0
        if not title and not url and not content:
            continue
        out.append({"title": title, "url": url, "content": content, "score": score})
        if len(out) >= max_results:
            break
    return out


async def _do_jina_search(query: str, max_results: int, api_key: str) -> list[dict]:
    base = _search_base_url()
    # 官方常用：https://s.jina.ai/?q=...  或 path 编码
    url = f"{base}/?q={quote(query)}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "X-Return-Format": "json",
        "User-Agent": "gsuid-core-jina-search/1.0",
    }
    timeout = aiohttp.ClientTimeout(total=_timeout_sec())
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status == 401 or resp.status == 403:
                raise JinaSearchError(f"Jina auth/quota HTTP {resp.status}: {body[:300]}")
            if resp.status == 429:
                raise JinaSearchError(f"Jina rate limit HTTP 429: {body[:300]}")
            if resp.status >= 400:
                raise JinaSearchError(f"Jina search HTTP {resp.status}: {body[:300]}")

            # 优先 JSON；非 JSON 时 body 已读，整段作单条 content
            try:
                data = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError, TypeError):
                text = body.strip()
                if not text:
                    return []
                return [
                    {
                        "title": "",
                        "url": "",
                        "content": text[:8000],
                        "score": 0.0,
                    }
                ]

            if isinstance(data, dict):
                code = data["code"] if "code" in data else None
                payload = data["data"] if "data" in data else None
                if code not in (None, 200, 0) and payload in (None, [], {}):
                    if "readableMessage" in data and data["readableMessage"] is not None:
                        msg = str(data["readableMessage"])
                    elif "message" in data and data["message"] is not None:
                        msg = str(data["message"])
                    else:
                        msg = str(data)[:200]
                    raise JinaSearchError(f"Jina search error code={code}: {msg}")

            return _normalize_items(data, max_results)


async def jina_search(
    query: str,
    max_results: Optional[int] = None,
) -> list[dict]:
    """
    使用 s.jina.ai 搜索。

    Raises:
        JinaSearchError: 未配置 Key 或全部 Key 失败
    """
    api_key_pool = _get_api_key_pool()
    if not api_key_pool:
        raise JinaSearchError(t("log.ai.websearch_jina_api_key_skip"))

    if max_results is None:
        try:
            max_results = int(jina_config.get_config("max_results").data or 10)
        except (TypeError, ValueError):
            max_results = 10

    tried: set[str] = set()
    last_err: Optional[Exception] = None
    while len(tried) < len(api_key_pool):
        api_key = _select_api_key([k for k in api_key_pool if k not in tried])
        if not api_key:
            break
        tried.add(api_key)
        try:
            results = await _do_jina_search(query, max_results, api_key)
            logger.info(t("log.ai.websearch_jina_search_query", query=query, p0=len(results)))
            return results
        except (JinaSearchError, aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
            last_err = e
            logger.warning(t("log.ai.websearch_jina_api_key_trying", p0=api_key[-4:]))
            continue

    msg = t("log.ai.websearch_jina_api_keys")
    if last_err:
        raise JinaSearchError(f"{msg}: {last_err}") from last_err
    raise JinaSearchError(msg)


def jina_search_configured() -> bool:
    """是否至少配置了一个 Jina API Key（搜索必填）。"""
    return bool(_get_api_key_pool())
