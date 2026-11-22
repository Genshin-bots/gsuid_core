"""通过 Parallel 的公开 MCP 端点进行网页搜索。"""

import re
import asyncio
from typing import TypedDict
from urllib.parse import urlsplit
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx
from mcp import McpError
from anyio import EndOfStream, BrokenResourceError, ClosedResourceError
from fastmcp import Client
from pydantic import BaseModel, ValidationError
from mcp.types import TextContent, CallToolResult, Implementation
from fastmcp.exceptions import ClientError, FastMCPError
from fastmcp.client.transports import StreamableHttpTransport
from mcp.client.streamable_http import StreamableHTTPError

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.version import __version__
from gsuid_core.ai_core.mcp.utils import sanitize_mcp_text

from .date_utils import normalize_published_date

_PARALLEL_MCP_URL = "https://mcp.parallel.ai/mcp"
_CLIENT_NAME = "gsuid-core"
_TIMEOUT_SECONDS = 30
_BOUNDARY_TAG_RE = re.compile(r"<\s*/?\s*(?:search_results|system|系统|instructions?)\b[^>]*>", re.IGNORECASE)


class _ParallelHit(BaseModel):
    url: str
    title: str | None = None
    publish_date: str | None = None
    excerpts: list[str] | None = None


class _ParallelResponse(BaseModel):
    results: list[_ParallelHit]


class ParallelSearchResult(TypedDict):
    title: str
    url: str
    content: str
    published_date: str
    score: float


def _clean_result_text(text: str, max_chars: int) -> str:
    # 剥离搜索与指令边界标签，保留正文里正常的比较符与泛型符号。
    cleaned = _BOUNDARY_TAG_RE.sub("", sanitize_mcp_text(text))
    return sanitize_mcp_text(cleaned, max_chars=max_chars)


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
    **_kwargs: object,
) -> httpx.AsyncClient:
    # 公开请求不跟随重定向（fastmcp 会传 follow_redirects=True）且不保留 Cookie。
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout if timeout is not None else _TIMEOUT_SECONDS,
        auth=auth,
        follow_redirects=False,
        max_redirects=0,
        cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
    )


def _iter_leaf_errors(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for item in exc.exceptions:
            leaves.extend(_iter_leaf_errors(item))
        return leaves
    return [exc]


def _summarize_request_error(exc: BaseException) -> str:
    parts: list[str] = []
    for leaf in _iter_leaf_errors(exc):
        raw = str(leaf).strip()
        first_line = raw.splitlines()[0] if raw else ""
        label = f"{type(leaf).__name__}: {first_line}" if first_line else type(leaf).__name__
        if label not in parts:
            parts.append(label)
    detail = "; ".join(parts)[:240]
    return f"Parallel MCP search request failed: {detail}" if detail else "Parallel MCP search request failed"


def _summarize_tool_error(result: CallToolResult) -> str:
    texts = [item.text.strip() for item in result.content if isinstance(item, TextContent) and item.text.strip()]
    detail = " ".join(texts)[:200]
    if detail:
        return f"Parallel MCP web_search returned a tool error: {detail}"
    return "Parallel MCP web_search returned a tool error"


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
    client = Client(
        transport,
        timeout=_TIMEOUT_SECONDS,
        init_timeout=_TIMEOUT_SECONDS,
        client_info=client_info,
    )
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            async with client:
                result = await client.call_tool_mcp("web_search", {"objective": query, "search_queries": [query]})
    except* (
        httpx.HTTPError,
        McpError,
        ClientError,
        FastMCPError,
        StreamableHTTPError,
        TimeoutError,
        OSError,
        ValueError,
        RuntimeError,
        BrokenResourceError,
        ClosedResourceError,
        EndOfStream,
    ) as exc:
        raise RuntimeError(_summarize_request_error(exc)) from exc
    finally:
        await client.close()
    if result.isError:
        raise RuntimeError(_summarize_tool_error(result))

    try:
        response = _ParallelResponse.model_validate(result.structuredContent, strict=True)
    except ValidationError as exc:
        raise ValueError("Parallel MCP web_search returned invalid results") from exc

    results: list[ParallelSearchResult] = []
    for hit in response.results:
        url = hit.url.strip()
        if not _is_source_url(url):
            continue
        excerpts = [text for text in (hit.excerpts or []) if text]
        results.append(
            {
                "title": _clean_result_text(hit.title or "", 300),
                "url": url,
                "content": _clean_result_text("\n\n".join(excerpts), 2000),
                "published_date": normalize_published_date(hit.publish_date),
                "score": 0.0,
            }
        )
    final = results[:max_results]
    logger.info(t("log.ai.websearch_parallel_search_query", query=query, p0=len(final)))
    return final
