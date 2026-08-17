"""Responses 的 hosted web_search：默认开，Chat Completions 永远走本地。

开关在 OpenAI 配置 ``remote_web_search``（默认 on）。
``request_method=responses`` 且开关 on 才挂 ``WebSearch``；
``chat_completions`` 无视开关，本轮只用 ``web_search_tool``。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic_ai.tools import Tool
from pydantic_ai.capabilities import WebSearch

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.register import find_tool_base
from gsuid_core.ai_core.configs.models import to_request_method
from gsuid_core.ai_core.agent_run.state import RunOnceState, _require_context
from gsuid_core.ai_core.configs.attribution import split_provider_config_name
from gsuid_core.ai_core.configs.openai_config import get_openai_config

LOCAL_WEB_SEARCH_TOOL = "web_search_tool"
HOSTED_WEB_SEARCH_TOOL = "web_search"


def remote_web_search_should_attach(
    *,
    enabled: bool,
    request_method: str,
    has_local_web_search: bool,
) -> bool:
    """Chat 永远不挂；Responses 且开关 on 且本轮池子有搜索工具才挂。"""
    return enabled and request_method == "responses" and has_local_web_search


def read_remote_web_search_flag(config_full_name: str | None) -> tuple[bool, str]:
    """读 OpenAI 配置，返回 ``(enabled, request_method)``。

    非 openai / 空配置名视为开关关、方法为 chat_completions。
    旧文件缺 key 时 ``get_config`` 会补模板默认 on。
    """
    if not config_full_name:
        return False, "chat_completions"
    provider, config_name = split_provider_config_name(config_full_name)
    if provider != "openai" or not config_name:
        return False, "chat_completions"
    oconfig = get_openai_config(config_name)
    enabled = str(oconfig.get_config("remote_web_search").data).strip().lower() == "on"
    method = to_request_method(str(oconfig.get_config("request_method").data))
    return enabled, method


def is_hosted_web_search_name(tool_name: str | None) -> bool:
    return (tool_name or "") == HOSTED_WEB_SEARCH_TOOL


def attach_remote_web_search(st: RunOnceState, config_full_name: str | None) -> Optional[WebSearch[Any]]:
    """满足条件则从函数工具池拿掉 ``web_search_tool``，返回 ``WebSearch`` capability。

    ``st.tool_names`` 仍保留原名，供渐进式召回 exclude / 日志使用。
    不满足条件返回 None，调用方不挂 capability。
    """
    enabled, method = read_remote_web_search_flag(config_full_name)
    has_local = LOCAL_WEB_SEARCH_TOOL in st.tool_names
    if not remote_web_search_should_attach(
        enabled=enabled,
        request_method=method,
        has_local_web_search=has_local,
    ):
        return None
    tb = find_tool_base(LOCAL_WEB_SEARCH_TOOL)
    if tb is None:
        return None
    local_tool: Tool[Any] = tb.tool
    st.tools = [t for t in st.tools if t.name != LOCAL_WEB_SEARCH_TOOL]
    _require_context(st).blocked_tool_names.add(LOCAL_WEB_SEARCH_TOOL)
    logger.info(i18n_t("log.agent.remote_web_search_attached"))
    return WebSearch(local=local_tool)
