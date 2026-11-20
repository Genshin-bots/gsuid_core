"""通过 Parallel 的公开 MCP 端点进行网页搜索。"""

import asyncio
from typing import TypedDict
from urllib.parse import urlsplit
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from mcp import McpError
from fastmcp import Client
from pydantic import BaseModel, ValidationError
from mcp.types import Implementation
from fastmcp.client.transports import StreamableHttpTransport

from gsuid_core.version import __version__
from gsuid_core.ai_core.mcp.utils import sanitize_mcp_text

from .date_utils import normalize_published_date

_PARALLEL_MCP_URL = "https://mcp.parallel.ai/mcp"
_CLIENT_NAME = "gsuid-core"
_TIMEOUT_SECONDS = 25


class _ParallelHit(BaseModel):
    url: str
    title: str | None = None
    publish_date: str | None = None
    excerpts: list[str]


class _ParallelResponse(BaseModel):
    results: list[_ParallelHit]


class ParallelSearchResult(TypedDict):
    title: str
    url: str
    content: str
    published_date: str
    score: float


def _clean_result_text(text: str, max_chars: int) -> str:
    # 外部内容不能插入搜索结果的边界标签。
    return sanitize_mcp_text(text, max_chars=max_chars).replace("<", "&lt;")


def _is_source_url(url: str) -> bool:
    if not url.lower().startswith(("https://", "http://")) or any(
        char.isspace() or not char.isprintable() or char in "<>" for char in url
    ):
        return False
    try:
        return bool(urlsplit(url).hostname)
    except ValueError:
        return False


def _create_anonymous_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    *,
    follow_redirects: bool = False,
) -> httpx.AsyncClient:
    # 公开请求不保留或回送服务器设置的 Cookie。
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout if timeout is not None else _TIMEOUT_SECONDS,
        auth=auth,
        follow_redirects=follow_redirects,
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
    )


async def parallel_search(query: str, max_results: int | None = None) -> list[ParallelSearchResult]:
    query = query.strip()
    if not query or (max_results is not None and max_results <= 0):
        return []

    transport = StreamableHttpTransport(
        _PARALLEL_MCP_URL,
        headers={"User-Agent": f"{_CLIENT_NAME}/{__version__}"},
        httpx_client_factory=_create_anonymous_http_client,
    )
    client_info = Implementation(name=_CLIENT_NAME, version=__version__)
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            async with Client(transport, client_info=client_info) as client:
                result = await client.call_tool_mcp("web_search", {"objective": query, "search_queries": [query]})
    except* (httpx.HTTPError, McpError, RuntimeError) as exc:
        raise RuntimeError("Parallel MCP search request failed") from exc
    if result.isError:
        raise RuntimeError("Parallel MCP web_search returned a tool error")

    try:
        response = _ParallelResponse.model_validate(result.structuredContent, strict=True)
    except ValidationError as exc:
        raise ValueError("Parallel MCP web_search returned invalid results") from exc

    results: list[ParallelSearchResult] = []
    for hit in response.results:
        if not _is_source_url(hit.url):
            continue
        results.append(
            {
                "title": _clean_result_text(hit.title or "", 300),
                "url": hit.url,
                "content": _clean_result_text("\n\n".join(hit.excerpts), 2000),
                "published_date": normalize_published_date(hit.publish_date),
                "score": 0.0,
            }
        )
    return results[:max_results]
