"""
MCP (Model Context Protocol) 客户端与服务端模块。

通过 PEP 562 ``__getattr__`` 延迟转发公开符号，避免导入 ``mcp.transport``、
``mcp.config_manager`` 等轻量配置子模块时把 ``fastmcp`` 拉进进程。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gsuid_core.ai_core.mcp.utils import (
        MCP_PROVIDER,
        get_mcp_tool_id,
        is_mcp_provider,
        cleanup_tempfile,
        build_mcp_arguments,
        parse_binary_result,
        get_mcp_tool_details,
        call_mcp_tool_checked,
        prepare_source_for_mcp,
        save_binary_to_tempfile,
        get_mcp_tool_id_optional,
        save_data_uri_to_tempfile,
    )
    from gsuid_core.ai_core.mcp.client import MCPClient, MCPToolInfo, MCPToolResult
    from gsuid_core.ai_core.mcp.server import (
        get_mcp_server,
        get_mcp_trigger_count,
        clear_mcp_token_verifiers,
        register_mcp_export_filter,
        get_mcp_exported_tool_count,
        register_mcp_event_enricher,
        register_mcp_token_verifier,
        unregister_mcp_export_filter,
        unregister_mcp_event_enricher,
        unregister_mcp_token_verifier,
    )
    from gsuid_core.ai_core.mcp.startup import (
        unregister_mcp_server,
        register_all_mcp_tools,
        register_single_mcp_server,
    )
    from gsuid_core.ai_core.mcp.transport import (
        MCP_TRANSPORT_SSE,
        MCP_HTTP_TRANSPORTS,
        MCP_TRANSPORT_STDIO,
        MCP_TRANSPORT_STREAMABLE_HTTP,
        detect_mcp_transport,
        is_http_mcp_transport,
        resolve_mcp_transport,
        normalize_mcp_transport,
    )
    from gsuid_core.ai_core.mcp.mcp_presets import MCP_PRESETS
    from gsuid_core.ai_core.mcp.config_manager import (
        MCPConfig,
        MCPConfigManager,
        MCPToolDefinition,
        parse_mcp_tool_id,
        format_mcp_tool_id,
        mcp_config_manager,
    )

_EXPORTS: dict[str, str] = {
    "MCPClient": "gsuid_core.ai_core.mcp.client",
    "MCPToolInfo": "gsuid_core.ai_core.mcp.client",
    "MCPToolResult": "gsuid_core.ai_core.mcp.client",
    "MCPConfig": "gsuid_core.ai_core.mcp.config_manager",
    "MCPToolDefinition": "gsuid_core.ai_core.mcp.config_manager",
    "MCPConfigManager": "gsuid_core.ai_core.mcp.config_manager",
    "mcp_config_manager": "gsuid_core.ai_core.mcp.config_manager",
    "parse_mcp_tool_id": "gsuid_core.ai_core.mcp.config_manager",
    "format_mcp_tool_id": "gsuid_core.ai_core.mcp.config_manager",
    "MCP_PRESETS": "gsuid_core.ai_core.mcp.mcp_presets",
    "MCP_TRANSPORT_STDIO": "gsuid_core.ai_core.mcp.transport",
    "MCP_TRANSPORT_SSE": "gsuid_core.ai_core.mcp.transport",
    "MCP_TRANSPORT_STREAMABLE_HTTP": "gsuid_core.ai_core.mcp.transport",
    "MCP_HTTP_TRANSPORTS": "gsuid_core.ai_core.mcp.transport",
    "normalize_mcp_transport": "gsuid_core.ai_core.mcp.transport",
    "detect_mcp_transport": "gsuid_core.ai_core.mcp.transport",
    "resolve_mcp_transport": "gsuid_core.ai_core.mcp.transport",
    "is_http_mcp_transport": "gsuid_core.ai_core.mcp.transport",
    "register_all_mcp_tools": "gsuid_core.ai_core.mcp.startup",
    "register_single_mcp_server": "gsuid_core.ai_core.mcp.startup",
    "unregister_mcp_server": "gsuid_core.ai_core.mcp.startup",
    "get_mcp_server": "gsuid_core.ai_core.mcp.server",
    "get_mcp_trigger_count": "gsuid_core.ai_core.mcp.server",
    "get_mcp_exported_tool_count": "gsuid_core.ai_core.mcp.server",
    "register_mcp_token_verifier": "gsuid_core.ai_core.mcp.server",
    "unregister_mcp_token_verifier": "gsuid_core.ai_core.mcp.server",
    "clear_mcp_token_verifiers": "gsuid_core.ai_core.mcp.server",
    "register_mcp_event_enricher": "gsuid_core.ai_core.mcp.server",
    "unregister_mcp_event_enricher": "gsuid_core.ai_core.mcp.server",
    "register_mcp_export_filter": "gsuid_core.ai_core.mcp.server",
    "unregister_mcp_export_filter": "gsuid_core.ai_core.mcp.server",
    "MCP_PROVIDER": "gsuid_core.ai_core.mcp.utils",
    "get_mcp_tool_id": "gsuid_core.ai_core.mcp.utils",
    "get_mcp_tool_id_optional": "gsuid_core.ai_core.mcp.utils",
    "get_mcp_tool_details": "gsuid_core.ai_core.mcp.utils",
    "build_mcp_arguments": "gsuid_core.ai_core.mcp.utils",
    "call_mcp_tool_checked": "gsuid_core.ai_core.mcp.utils",
    "save_binary_to_tempfile": "gsuid_core.ai_core.mcp.utils",
    "cleanup_tempfile": "gsuid_core.ai_core.mcp.utils",
    "parse_binary_result": "gsuid_core.ai_core.mcp.utils",
    "save_data_uri_to_tempfile": "gsuid_core.ai_core.mcp.utils",
    "prepare_source_for_mcp": "gsuid_core.ai_core.mcp.utils",
    "is_mcp_provider": "gsuid_core.ai_core.mcp.utils",
}

__all__ = [
    "MCPClient",
    "MCPToolInfo",
    "MCPToolResult",
    "MCPConfig",
    "MCPToolDefinition",
    "MCPConfigManager",
    "mcp_config_manager",
    "parse_mcp_tool_id",
    "format_mcp_tool_id",
    "MCP_PRESETS",
    "MCP_TRANSPORT_STDIO",
    "MCP_TRANSPORT_SSE",
    "MCP_TRANSPORT_STREAMABLE_HTTP",
    "MCP_HTTP_TRANSPORTS",
    "normalize_mcp_transport",
    "detect_mcp_transport",
    "resolve_mcp_transport",
    "is_http_mcp_transport",
    "register_all_mcp_tools",
    "register_single_mcp_server",
    "unregister_mcp_server",
    "get_mcp_server",
    "get_mcp_trigger_count",
    "get_mcp_exported_tool_count",
    "register_mcp_token_verifier",
    "unregister_mcp_token_verifier",
    "clear_mcp_token_verifiers",
    "register_mcp_event_enricher",
    "unregister_mcp_event_enricher",
    "register_mcp_export_filter",
    "unregister_mcp_export_filter",
    "MCP_PROVIDER",
    "get_mcp_tool_id",
    "get_mcp_tool_id_optional",
    "get_mcp_tool_details",
    "build_mcp_arguments",
    "call_mcp_tool_checked",
    "save_binary_to_tempfile",
    "cleanup_tempfile",
    "parse_binary_result",
    "save_data_uri_to_tempfile",
    "prepare_source_for_mcp",
    "is_mcp_provider",
]


def __getattr__(name: str) -> object:
    if name in _EXPORTS:
        module = importlib.import_module(_EXPORTS[name])
        value = module.__dict__[name]
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
