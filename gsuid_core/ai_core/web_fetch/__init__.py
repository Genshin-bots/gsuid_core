"""
Web Fetch 模块

提供网页内容抓取并转换为 Markdown 格式的功能。
使用 aiohttp 进行异步 HTTP 请求，使用 BeautifulSoup 清理 HTML，
使用 markdownify 将 HTML 转换为 Markdown。

运行时参数（proxy / timeout / UA 等）来自 ``web_fetch_config``
（``data/ai_core/web_fetch_config.json``，控制台「AI 基本配置 → 网页抓取」可改）。
"""

from __future__ import annotations

import random
import asyncio
import itertools
import threading
from typing import Any, Dict, List, Tuple, Optional

import aiohttp
from bs4 import Comment, BeautifulSoup
from markdownify import markdownify as md

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import ai_config, jina_config, web_fetch_config

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 20
MAX_CONTENT_LENGTH = 100_000
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024

DEFAULT_HEADERS = {
    "User-Agent": _DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_balance_lock = threading.Lock()
_balance_counter = itertools.count()


def _cfg_data(key: str) -> Any:
    """读 web_fetch_config 项的 data。"""
    return web_fetch_config.get_config(key).data


def _as_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return int(value)
    if isinstance(value, float):
        return int(value)
    return default


def _resolve_runtime_settings(
    timeout: Optional[int],
    max_length: Optional[int],
) -> Tuple[int, int, int, Dict[str, str], Optional[str], bool]:
    """合并调用参数与配置文件，返回运行时抓取参数。

    Returns:
        (timeout_sec, max_md_chars, max_download_bytes, headers, proxy, trust_env)
    """
    try:
        timeout_sec = _as_int(
            timeout if timeout is not None else _cfg_data("timeout"),
            DEFAULT_TIMEOUT,
        )
    except (TypeError, ValueError):
        timeout_sec = DEFAULT_TIMEOUT
    timeout_sec = max(1, min(timeout_sec, 300))

    try:
        max_md = _as_int(
            max_length if max_length is not None else _cfg_data("max_content_length"),
            MAX_CONTENT_LENGTH,
        )
    except (TypeError, ValueError):
        max_md = MAX_CONTENT_LENGTH
    max_md = max(1000, max_md)

    try:
        max_mb = _as_int(_cfg_data("max_download_mb"), 5)
    except (TypeError, ValueError):
        max_mb = 5
    max_mb = max(1, min(max_mb, 50))
    max_dl = max_mb * 1024 * 1024

    ua = str(_cfg_data("user_agent") or _DEFAULT_UA).strip() or _DEFAULT_UA
    accept_lang = str(_cfg_data("accept_language") or DEFAULT_HEADERS["Accept-Language"]).strip()
    headers = {
        "User-Agent": ua,
        "Accept": DEFAULT_HEADERS["Accept"],
        "Accept-Language": accept_lang,
    }

    proxy_raw = str(_cfg_data("proxy") or "").strip()
    proxy: Optional[str] = proxy_raw or None

    trust_env_raw = _cfg_data("trust_env")
    if isinstance(trust_env_raw, str):
        trust_env = trust_env_raw.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(trust_env_raw, bool):
        trust_env = trust_env_raw
    else:
        trust_env = True

    return timeout_sec, max_md, max_dl, headers, proxy, trust_env


def _normalize_fetch_provider(raw: Any) -> str:
    s = str(raw or "Jina").strip()
    lowered = s.lower()
    if lowered in ("jina", "r.jina.ai", "reader") or s == "Jina":
        return "Jina"
    if lowered in ("local", "direct", "aiohttp") or s == "local":
        return "local"
    return "Jina"


def _webfetch_provider() -> str:
    """主用源：local | Jina；控制台热改，无需重启。默认 Jina。"""
    raw = ai_config.get_config("webfetch_provider", "Jina").data
    return _normalize_fetch_provider(raw)


def _webfetch_lb_strategy() -> str:
    """none | error_switch | auto_balance。"""
    raw = ai_config.get_config("webfetch_lb_strategy", "error_switch").data
    s = str(raw or "error_switch").strip().lower()
    if s in ("none", "error_switch", "auto_balance"):
        return s
    mapping = {
        "无": "none",
        "错误切换": "error_switch",
        "自动分流": "auto_balance",
        "failover": "error_switch",
        "round_robin": "auto_balance",
        "rr": "auto_balance",
    }
    return mapping[s] if s in mapping else "error_switch"


def _webfetch_fallback_order() -> List[str]:
    """备用源列表；未配置/空列表时默认 local。"""
    raw = ai_config.get_config("webfetch_fallback_order").data
    if isinstance(raw, list) and raw:
        out: List[str] = []
        for item in raw:
            p = _normalize_fetch_provider(item)
            if p not in out:
                out.append(p)
        return out
    return ["local"]


def _build_fetch_chain() -> List[str]:
    primary = _webfetch_provider()
    strategy = _webfetch_lb_strategy()
    if strategy == "none":
        return [primary]
    chain: List[str] = [primary]
    for name in _webfetch_fallback_order():
        if name != primary and name not in chain:
            chain.append(name)
    return chain


def _rotate_chain(chain: List[str]) -> List[str]:
    if len(chain) <= 1:
        return chain
    with _balance_lock:
        start = next(_balance_counter) % len(chain)
    return chain[start:] + chain[:start]


def _jina_api_keys() -> list[Optional[str]]:
    raw_keys = jina_config.get_config("api_key").data
    api_keys: list[str] = []
    if isinstance(raw_keys, list):
        api_keys = [k for k in raw_keys if isinstance(k, str) and k]
    elif isinstance(raw_keys, str) and raw_keys:
        api_keys = [raw_keys]
    # 无 Key 也允许匿名请求
    keys_to_try: list[Optional[str]] = list(api_keys) if api_keys else [None]
    if len(keys_to_try) > 1:
        random.shuffle(keys_to_try)
    return keys_to_try


async def _fetch_via_jina(url: str, max_md: int) -> str:
    """经 r.jina.ai 抓取；API Key 可选（无 Key 有额度限制）。"""
    base = str(jina_config.get_config("reader_base_url").data or "https://r.jina.ai").strip().rstrip("/")
    if not base:
        base = "https://r.jina.ai"
    try:
        timeout_sec = max(5, min(180, _as_int(jina_config.get_config("timeout").data, 30)))
    except (TypeError, ValueError):
        timeout_sec = 30

    keys_to_try = _jina_api_keys()
    auth_mode = "with_api_key" if any(keys_to_try) else "anonymous"
    logger.info(
        t(
            "log.ai.webfetch_start",
            provider="Jina",
            url=url,
            detail=f"endpoint={base} auth={auth_mode}",
        )
    )

    target = f"{base}/{url}"
    last_err: Optional[Exception] = None

    for api_key in keys_to_try:
        headers = {
            "Accept": "text/markdown, text/plain, */*",
            "X-Return-Format": "markdown",
            "User-Agent": "gsuid-core-jina-reader/1.0",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout_sec)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.get(target, headers=headers) as resp:
                    body = await resp.text()
                    if resp.status == 429:
                        last_err = ValueError(f"Jina rate limit 429: {body[:200]}")
                        continue
                    if resp.status in (401, 403) and api_key:
                        last_err = ValueError(f"Jina auth {resp.status}: {body[:200]}")
                        continue
                    if resp.status >= 400:
                        raise ValueError(
                            t(
                                "Jina 抓取失败，状态码: {p0}，URL: {url}",
                                p0=resp.status,
                                url=url,
                            )
                        )
                    result = (body or "").strip()
                    if not result:
                        last_err = ValueError(t("抓取结果为空，切换下一源"))
                        continue
                    if len(result) > max_md:
                        result = result[:max_md] + "\n\n...(内容已截断)"
                    logger.info(t("log.ai.webfetch_jina_ok", url=url, p0=len(result)))
                    return result
        except ValueError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            last_err = e
            logger.warning(t("log.ai.webfetch_jina_try_fail", url=url, e=e))
            continue

    if last_err is not None:
        raise ValueError(t("Jina 抓取失败: {e}", e=last_err)) from last_err
    raise ValueError(t("Jina 抓取失败: empty response"))


def _html_to_markdown(html_content: str, url: str) -> str:
    """清理 HTML 并转 Markdown；解析失败抛 ValueError。"""
    try:
        soup = BeautifulSoup(html_content, "lxml")
        for tag_name in (
            "script",
            "style",
            "noscript",
            "svg",
            "iframe",
            "nav",
            "footer",
            "header",
        ):
            for tag in soup.find_all(tag_name):
                tag.decompose()

        for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        main_content = (
            soup.find("article")
            or soup.find("main")
            or soup.find(attrs={"role": "main"})
            or soup.find("div", class_="markdown-body")
            or soup.find("div", id="content")
            or soup.find("body")
            or soup
        )
        cleaned_html = str(main_content)
    except (TypeError, ValueError, AttributeError) as e:
        logger.error(t("log.ai.webfetch_html_cleanup_url_fail", url=url, e=e))
        raise ValueError(t("HTML 清理失败: {e}", e=e)) from e

    try:
        markdown_content = md(cleaned_html, heading_style="ATX", bullets="-")
    except (TypeError, ValueError, AttributeError, RecursionError) as e:
        logger.error(t("log.ai.webfetch_html_markdown_conversion_fail", url=url, e=e))
        raise ValueError(t("HTML 转 Markdown 失败: {e}", e=e)) from e

    lines = markdown_content.split("\n")
    cleaned_lines: list[str] = []
    empty_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            empty_count += 1
            if empty_count <= 2:
                cleaned_lines.append("")
        else:
            empty_count = 0
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


async def _fetch_via_local(
    url: str,
    timeout: Optional[int] = None,
    max_length: Optional[int] = None,
) -> str:
    """本机 aiohttp 直连 + HTML→Markdown。"""
    timeout_sec, max_md, max_dl, headers, proxy, trust_env = _resolve_runtime_settings(timeout, max_length)

    proxy_hint = f"proxy={proxy[:60]}" if proxy else "proxy=off"
    logger.info(
        t(
            "log.ai.webfetch_start",
            provider="local",
            url=url,
            detail=f"{proxy_hint} timeout={timeout_sec}s",
        )
    )
    if proxy:
        logger.debug(t("log.ai.webfetch_using_proxy", proxy=proxy[:80]))

    try:
        client_timeout = aiohttp.ClientTimeout(
            total=timeout_sec,
            connect=min(10, timeout_sec),
            sock_connect=min(10, timeout_sec),
            sock_read=timeout_sec,
        )
        async with aiohttp.ClientSession(
            headers=headers,
            timeout=client_timeout,
            trust_env=trust_env,
        ) as session:
            async with session.get(url, proxy=proxy) as response:
                if response.status != 200:
                    raise ValueError(t("请求失败，状态码: {p0}，URL: {url}", p0=response.status, url=url))

                content_type = response.content_type or ""
                if "text" not in content_type and "html" not in content_type:
                    raise ValueError(
                        t(
                            "不支持的内容类型: {content_type}，仅支持 HTML/文本页面",
                            content_type=content_type,
                        )
                    )

                cl_hdr = response.headers.get("Content-Length")
                if cl_hdr is not None and cl_hdr.isdigit() and int(cl_hdr) > max_dl:
                    raise ValueError(
                        t(
                            "响应体过大（Content-Length={cl}），上限 {max_b} 字节",
                            cl=cl_hdr,
                            max_b=max_dl,
                        )
                    )

                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    size += len(chunk)
                    if size > max_dl:
                        raise ValueError(
                            t(
                                "响应体过大（已读 {size} 字节），上限 {max_b} 字节",
                                size=size,
                                max_b=max_dl,
                            )
                        )
                    chunks.append(chunk)
                raw = b"".join(chunks)
                html_content = raw.decode(response.charset or "utf-8", errors="replace")

    except asyncio.TimeoutError as e:
        logger.error(t("log.ai.webfetch_network_request_url_fail", url=url, e=e))
        raise ValueError(t("网页抓取超时（{timeout}s）: {url}", timeout=timeout_sec, url=url)) from e
    except aiohttp.ClientError as e:
        logger.error(t("log.ai.webfetch_network_request_url_fail", url=url, e=e))
        raise ValueError(t("网络请求失败: {e}", e=e)) from e

    result = _html_to_markdown(html_content, url)
    if not result:
        raise ValueError(t("抓取结果为空，切换下一源"))

    if len(result) > max_md:
        original_len = len(result)
        result = result[:max_md] + "\n\n...(内容已截断)"
        logger.warning(
            t(
                "log.ai.webfetch_content_long_truncated",
                url=url,
                p0=original_len,
                max_length=max_md,
            )
        )

    logger.info(t("log.ai.webfetch_local_ok", url=url, p0=len(result)))
    return result


async def _invoke_fetch_provider(
    provider: str,
    url: str,
    timeout: Optional[int],
    max_length: Optional[int],
    max_md: int,
) -> str:
    if provider == "Jina":
        return await _fetch_via_jina(url, max_md)
    return await _fetch_via_local(url, timeout=timeout, max_length=max_length)


async def fetch_webpage_as_markdown(
    url: str,
    timeout: Optional[int] = None,
    max_length: Optional[int] = None,
) -> str:
    """
    抓取指定 URL 的网页内容并转换为 Markdown 格式。

    按 ``webfetch_provider`` + ``webfetch_lb_strategy`` 在 Jina / local 间切换或分流。
    配置热读，控制台修改后无需重启。

    Args:
        url: 要抓取的网页 URL
        timeout: 请求超时（秒）；``None`` 时读配置（默认 20）
        max_length: 返回 Markdown 最大字符数；``None`` 时读配置

    Returns:
        转换后的 Markdown 文本内容

    Raises:
        ValueError: URL 无效、响应异常、网络/超时失败
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError(t("无效的 URL: {url}，必须以 http:// 或 https:// 开头", url=url))

    _, max_md, _, _, _, _ = _resolve_runtime_settings(timeout, max_length)

    strategy = _webfetch_lb_strategy()
    chain = _build_fetch_chain()
    if strategy == "auto_balance":
        chain = _rotate_chain(chain)

    errors: list[str] = []
    for idx, provider in enumerate(chain):
        try:
            result = await _invoke_fetch_provider(provider, url, timeout, max_length, max_md)
            if not result.strip():
                raise ValueError(t("抓取结果为空，切换下一源"))
            if idx > 0:
                logger.info(
                    t(
                        "log.ai.webfetch_failover_ok",
                        provider=provider,
                        url=url,
                        p0=len(result),
                    )
                )
            return result
        except (ValueError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            errors.append(f"{provider}: {e}")
            logger.warning(
                t(
                    "log.ai.webfetch_provider_fail",
                    provider=provider,
                    url=url,
                    e=e,
                    strategy=strategy,
                )
            )
            if strategy == "none":
                break
            continue

    detail = "; ".join(errors) if errors else "n/a"
    logger.error(t("log.ai.webfetch_all_providers_failed", url=url, p0=detail))
    raise ValueError(t("网页抓取全部源失败: {p0}", p0=detail))


__all__ = ["fetch_webpage_as_markdown", "DEFAULT_TIMEOUT", "MAX_CONTENT_LENGTH"]
