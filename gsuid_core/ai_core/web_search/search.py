"""
Web Search 公共 API 模块

提供统一的 web 搜索接口，根据用户配置自动选择搜索引擎（Tavily / Exa / Jina / AnySearch / MCP）。
支持多源策略：
- none：仅主用源
- error_switch：主用失败后按备用顺序切换（默认）
- auto_balance：在已配置源之间轮询分发

配置运行时读取，控制台修改后无需重启。
"""

from __future__ import annotations

import json
import itertools
import threading
from typing import Any

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.mcp.utils import (
    is_mcp_provider,
    sanitize_mcp_text,
    build_mcp_arguments,
    call_mcp_tool_checked,
    get_mcp_tool_id_optional,
)
from gsuid_core.ai_core.configs.ai_config import (
    ai_config,
    exa_config,
    tavily_config,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

from .exa_search import exa_search
from .jina_search import jina_search, jina_search_configured
from .tavily_search import (
    tavily_search,
    tavily_search_with_context,
)
from .anysearch_search import anysearch_search, anysearch_search_configured

# 单条 MCP 原始返回兜底透传时的最大字符数，避免一次搜索把上下文吃满
_MAX_MCP_RAW_CHARS = 4000

_balance_lock = threading.Lock()
_balance_counter = itertools.count()

# 默认候选顺序（主用源会插到队首）。AnySearch 可匿名，未配置 Key 时仍可用。
_DEFAULT_PROVIDER = "AnySearch"
_DEFAULT_PROVIDER_ORDER = ("AnySearch", "Tavily", "Exa", "Jina", "MCP")


class ProviderEmptyResultError(RuntimeError):
    """提供方返回空结果，供多源策略切换下一源。"""


def _get_provider() -> str:
    """主用搜索引擎。未填或主用未就绪（无 Key 等）时落到 AnySearch 匿名额度。"""
    raw = ai_config.get_config("websearch_provider").data or _DEFAULT_PROVIDER
    name = str(raw).strip() or _DEFAULT_PROVIDER
    if _provider_configured(name):
        return name
    return _DEFAULT_PROVIDER


def _get_lb_strategy() -> str:
    """多源策略：none | error_switch | auto_balance。"""
    raw = ai_config.get_config("websearch_lb_strategy", "error_switch").data
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


def _key_pool(config_obj: StringConfig, key: str = "api_key") -> list[str]:
    raw = config_obj.get_config(key).data
    if isinstance(raw, list):
        return [k for k in raw if isinstance(k, str) and k]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def _provider_configured(provider: str) -> bool:
    """判断该源是否已具备最小可用配置。"""
    name = provider.strip()
    if name == "Tavily":
        return bool(_key_pool(tavily_config))
    if name == "Exa":
        return bool(_key_pool(exa_config))
    if name == "Jina":
        return jina_search_configured()
    if name == "AnySearch":
        return anysearch_search_configured()
    if is_mcp_provider(name) or name.upper() == "MCP":
        return bool(get_mcp_tool_id_optional("websearch_mcp_tool_id"))
    return False


def _fallback_order_from_config() -> list[str]:
    """用户配置的备用顺序；空则用默认全集。"""
    raw = ai_config.get_config("websearch_fallback_order").data
    if isinstance(raw, list) and raw:
        out: list[str] = []
        for item in raw:
            s = str(item).strip()
            if s and s not in out:
                out.append(s)
        return out
    return list(_DEFAULT_PROVIDER_ORDER)


def _build_provider_chain() -> list[str]:
    """
    构建本次搜索的提供方链。

    - none：仅主用
    - error_switch / auto_balance：主用 + 已配置的其它源（按备用顺序）
    """
    primary = _get_provider()
    strategy = _get_lb_strategy()
    if strategy == "none":
        return [primary]

    chain: list[str] = [primary]
    for name in _fallback_order_from_config():
        if name == primary or name in chain:
            continue
        # 只纳入已配置源；主用即使未配置也保留，便于报错信息
        if _provider_configured(name):
            chain.append(name)
    return chain


def _rotate_chain(chain: list[str]) -> list[str]:
    if len(chain) <= 1:
        return chain
    with _balance_lock:
        start = next(_balance_counter) % len(chain)
    return chain[start:] + chain[:start]


async def _mcp_search(query: str, max_results: int | None = None) -> list[dict]:
    from gsuid_core.ai_core.mcp.utils import get_mcp_tool_id

    mcp_tool_id = get_mcp_tool_id("websearch_mcp_tool_id", "Web Search")
    arguments = build_mcp_arguments(
        "websearch_mcp_tool_id",
        {"query": query, "max_results": max_results},
    )
    result = await call_mcp_tool_checked(mcp_tool_id, arguments, "Web Search")
    return _parse_mcp_search_result(result.text, max_results)


def _parse_mcp_search_result(raw_text: str, max_results: int | None = None) -> list[dict]:
    cleaned = sanitize_mcp_text(raw_text)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.debug(t("log.ai.websearch_mcp_non_json_return", p0=len(cleaned)))
        return [
            {
                "title": "",
                "url": "",
                "content": sanitize_mcp_text(raw_text, max_chars=_MAX_MCP_RAW_CHARS),
                "score": 0.0,
            }
        ]

    logger.debug(t("log.ai.websearch_mcp_structured_return", p0=cleaned[:500]))

    results: Any
    if isinstance(data, list):
        results = data
    elif isinstance(data, dict):
        if "organic" in data:
            results = data["organic"]
        elif "results" in data:
            results = data["results"]
        else:
            results = [data]
    else:
        results = [data]

    if not isinstance(results, list):
        results = [results]

    if max_results is not None and len(results) > max_results:
        results = results[:max_results]

    normalized: list[dict] = []
    for item in results:
        if isinstance(item, dict):
            title = item["title"] if "title" in item else ""
            url = item["url"] if "url" in item else ""
            if "content" in item:
                content = item["content"]
            elif "snippet" in item:
                content = item["snippet"]
            else:
                content = ""
            score = item["score"] if "score" in item else 0.0
            normalized.append(
                {
                    "title": title,
                    "url": url,
                    "content": content,
                    "score": score,
                }
            )
        else:
            normalized.append({"title": str(item), "url": "", "content": "", "score": 0.0})

    return normalized


async def _invoke_provider(
    provider: str,
    query: str,
    max_results: int | None,
    *,
    with_context: bool = False,
) -> list[dict] | dict:
    """调用单一提供方；失败抛异常。"""
    name = provider.strip()
    if name == "Exa":
        results = await exa_search(query=query, max_results=max_results)
        return {"results": results, "answer": None} if with_context else results

    if name == "Jina":
        results = await jina_search(query=query, max_results=max_results)
        return {"results": results, "answer": None} if with_context else results

    if name == "AnySearch":
        results = await anysearch_search(query=query, max_results=max_results)
        return {"results": results, "answer": None} if with_context else results

    if is_mcp_provider(name) or name.upper() == "MCP":
        results = await _mcp_search(query=query, max_results=max_results)
        return {"results": results, "answer": None} if with_context else results

    if name == "Tavily":
        if with_context:
            return await tavily_search_with_context(
                query=query, max_results=max_results if max_results is not None else 5
            )
        return await tavily_search(query=query, max_results=max_results)

    # 未识别名字走 AnySearch 匿名保底，避免空配置搜不到
    results = await anysearch_search(query=query, max_results=max_results)
    return {"results": results, "answer": None} if with_context else results


def _ensure_non_empty_list(results: list[dict]) -> list[dict]:
    if not results:
        raise ProviderEmptyResultError(t("搜索结果为空，切换下一源"))
    return results


def _ensure_non_empty_context(result: dict) -> dict:
    results = result["results"] if "results" in result and isinstance(result["results"], list) else []
    answer = result["answer"] if "answer" in result else None
    if not results and not answer:
        raise ProviderEmptyResultError(t("搜索结果为空，切换下一源"))
    return result


async def web_search(
    query: str,
    max_results: int | None = None,
) -> list[dict]:
    """
    统一的 web 搜索接口。

    按 ``websearch_lb_strategy`` 在多源间切换/分流；配置热读，无需重启。
    异常或空结果在 error_switch/auto_balance 下会尝试下一源。
    """
    strategy = _get_lb_strategy()
    chain = _build_provider_chain()
    if strategy == "auto_balance":
        configured = [p for p in chain if _provider_configured(p)]
        chain = _rotate_chain(configured or chain)

    errors: list[str] = []
    for idx, provider in enumerate(chain):
        try:
            raw = await _invoke_provider(provider, query, max_results, with_context=False)
            if not isinstance(raw, list):
                raise TypeError(f"{provider} returned non-list: {type(raw).__name__}")
            results = _ensure_non_empty_list(raw)
            if idx > 0:
                logger.info(
                    t(
                        "log.ai.websearch_failover_ok",
                        provider=provider,
                        query=query,
                        p0=len(results),
                    )
                )
            return results
        except (RuntimeError, ProviderEmptyResultError, TypeError, ValueError, OSError) as e:
            errors.append(f"{provider}: {e}")
            logger.warning(
                t(
                    "log.ai.websearch_provider_fail",
                    provider=provider,
                    e=e,
                    strategy=strategy,
                )
            )
            if strategy == "none":
                break
            continue

    logger.error(
        t(
            "log.ai.websearch_all_providers_failed",
            query=query,
            p0="; ".join(errors) if errors else "n/a",
        )
    )
    return []


async def web_search_with_context(
    query: str,
    max_results: int = 5,
) -> dict:
    """
    统一的带上下文 web 搜索接口（Tavily 可返回 answer；其它源 answer=None）。
    """
    strategy = _get_lb_strategy()
    chain = _build_provider_chain()
    if strategy == "auto_balance":
        configured = [p for p in chain if _provider_configured(p)]
        chain = _rotate_chain(configured or chain)

    errors: list[str] = []
    for idx, provider in enumerate(chain):
        try:
            raw = await _invoke_provider(provider, query, max_results, with_context=True)
            if not isinstance(raw, dict):
                raise TypeError(f"{provider} returned non-dict: {type(raw).__name__}")
            result = _ensure_non_empty_context(raw)
            if idx > 0:
                results_list = result["results"] if isinstance(result["results"], list) else []
                logger.info(
                    t(
                        "log.ai.websearch_failover_ok",
                        provider=provider,
                        query=query,
                        p0=len(results_list),
                    )
                )
            return result
        except (RuntimeError, ProviderEmptyResultError, TypeError, ValueError, OSError) as e:
            errors.append(f"{provider}: {e}")
            logger.warning(
                t(
                    "log.ai.websearch_provider_fail",
                    provider=provider,
                    e=e,
                    strategy=strategy,
                )
            )
            if strategy == "none":
                break
            continue

    logger.error(
        t(
            "log.ai.websearch_all_providers_failed",
            query=query,
            p0="; ".join(errors) if errors else "n/a",
        )
    )
    return {"results": [], "answer": None}
