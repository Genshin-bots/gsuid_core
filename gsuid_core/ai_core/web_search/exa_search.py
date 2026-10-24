"""Exa 网页搜索与按 URL 抓取。

api_key 可以配多把，失败后换一把重试。
不传 type：配置文件里的 search_type 不读取。
httpx 的连接失败和超时同样换 Key。单次请求 30 秒到点就取消，短于工具外层的 100 秒。
"""

import random
import asyncio
from typing import Literal, TypeVar, NoReturn, Optional
from collections.abc import Callable, Awaitable

import httpx
from exa_py import AsyncExa
from exa_py.api import Result, SearchResponse, ContentsOptions, TextContentsOptions

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import exa_config

from .date_utils import normalize_published_date

_EXA_TIMEOUT_SEC = 30
_EXA_TEXT_MAX = 1_000_000
_Kind = Literal["search", "fetch"]
_T = TypeVar("_T")

_RETRYABLE: tuple[type[Exception], ...] = (
    RuntimeError,
    OSError,
    ValueError,
    TypeError,
    httpx.HTTPError,
)


def _get_api_key_pool() -> list[str]:
    """
    获取 api_key 池

    Returns:
        api_key 列表，如果是单个字符串则转换为单元素列表
    """
    api_key_data = exa_config.get_config("api_key").data

    if isinstance(api_key_data, list):
        return [k for k in api_key_data if k]
    elif isinstance(api_key_data, str) and api_key_data:
        return [api_key_data]
    else:
        return []


def _select_api_key(api_key_pool: list[str]) -> Optional[str]:
    """
    从 api_key 池中选择一个 api_key（随机选择）

    Args:
        api_key_pool: api_key 列表

    Returns:
        选中的 api_key，如果池为空则返回 None
    """
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


async def _with_keys(
    kind: _Kind,
    pool: list[str],
    call: Callable[[AsyncExa], Awaitable[_T]],
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
        exa = AsyncExa(api_key=api_key)
        try:
            # SDK 的 httpx 超时是 600 秒，工具外层 100 秒会先取消，来不及换 Key。
            return await asyncio.wait_for(call(exa), _EXA_TIMEOUT_SEC)
        except _RETRYABLE as e:
            _log_key_failure(kind, api_key, url, e)
            last_err = e
    _raise_all_failed(kind, last_err)


async def _do_exa_search(
    exa: AsyncExa,
    query: str,
    max_results: int,
) -> list[dict]:
    """
    执行 Exa 搜索的内部方法

    Args:
        exa: Exa 客户端
        query: 搜索查询关键词
        max_results: 最大返回结果数量

    Returns:
        搜索结果列表
    """
    contents: ContentsOptions = {
        "highlights": True,
        "text": {"max_characters": 1000},
    }
    response = await exa.search(
        query,
        num_results=max_results,
        contents=contents,
    )

    results = []
    for item in response.results:
        results.append(
            {
                "title": item.title or "",
                "url": item.url or "",
                "content": _extract_hit_content(item),
                "published_date": _published_date(item.published_date),
                "score": item.score if item.score is not None else 0.0,
            }
        )

    return results


def _extract_hit_content(item: Result) -> str:
    highlighted = _highlight_text(item)
    if highlighted:
        return highlighted
    if item.text:
        return item.text.strip()
    return ""


def _highlight_text(item: Result) -> str:
    pieces = item.highlights
    if not pieces:
        return ""
    lines: list[str] = []
    for piece in pieces:
        text = piece.strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _published_date(raw: str | None) -> str:
    return normalize_published_date(raw)


async def exa_search(
    query: str,
    max_results: Optional[int] = None,
) -> list[dict]:
    """
    使用 Exa API 进行 web 搜索

    支持 api_key 池配置，会自动轮询尝试不同的 api_key。

    Args:
        query: 搜索查询关键词
        max_results: 最大返回结果数量，默认10条

    Returns:
        搜索结果列表，每条包含 title、url、content、score 等字段

    Example:
        >>> results = await exa_search("Python 教程")
        >>> for r in results:
        ...     print(r["title"], r["url"])
    """
    api_key_pool = _get_api_key_pool()
    if not api_key_pool:
        _raise_no_key("search")

    if max_results is None:
        max_results = int(exa_config.get_config("max_results").data or "10")

    async def _call(exa: AsyncExa) -> list[dict]:
        return await _do_exa_search(exa, query, max_results)

    return await _with_keys("search", api_key_pool, _call)


def _text_limit(max_chars: int) -> int:
    if max_chars < 1:
        return 1
    if max_chars > _EXA_TEXT_MAX:
        return _EXA_TEXT_MAX
    return max_chars


def _text_from_contents(response: SearchResponse[Result]) -> str:
    statuses = response.statuses
    if statuses:
        saw_success = False
        for item in statuses:
            if item.status == "success":
                saw_success = True
                break
        if not saw_success:
            return ""
    if not response.results:
        return ""
    text = response.results[0].text
    if text is None:
        return ""
    return text.strip()


async def exa_fetch_markdown(url: str, max_chars: int) -> str:
    """用 Exa Contents 读取指定 URL 的正文文本（清洗后的纯文本，不保证为 Markdown 格式）。
    空正文抛 ValueError，供抓取链换源。
    """
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

    async def _call(exa: AsyncExa) -> str:
        text_opts: TextContentsOptions = {"max_characters": limit}
        raw = await exa.get_contents([url], text=text_opts)
        if not isinstance(raw, SearchResponse):
            return ""
        return _text_from_contents(raw)

    text = await _with_keys("fetch", api_key_pool, _call, url=url)
    if not text:
        raise ValueError(t("抓取结果为空，切换下一源"))
    if len(text) > limit:
        text = text[:limit] + "\n\n...(内容已截断)"
    logger.info(t("log.ai.webfetch_exa_ok", url=url, p0=len(text)))
    return text
