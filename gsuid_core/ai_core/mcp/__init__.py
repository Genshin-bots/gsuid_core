"""
MCP (Model Context Protocol) 客户端模块

提供通用的 MCP 客户端功能，用于连接和调用 MCP 服务器。
基于 fastmcp 实现，支持 stdio、sse 和 Streamable HTTP 三种传输方式，异步操作。

支持用户通过 WebConsole API 自由添加 MCP 服务器配置，
框架启动时自动连接 MCP 服务器并将工具注册为 AI 工具。

Example (stdio):
    >>> from gsuid_core.ai_core.mcp import MCPClient
    >>> client = MCPClient(
    ...     name="MiniMax",
    ...     command="uvx",
    ...     args=["minimax-coding-plan-mcp"],
    ...     env={"MINIMAX_API_KEY": "your_key"},
    ... )
    >>> tools = await client.list_tools()
    >>> result = await client.call_tool("web_search", {"query": "Python"})

Example (streamable_http):
    >>> client = MCPClient(
    ...     name="Example",
    ...     transport="streamable_http",
    ...     url="https://example.com/mcp",
    ...     headers={"Authorization": "Bearer your_key"},
    ... )
    >>> tools = await client.list_tools()
"""

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

__all__ = [
    "MCPClient",
    "MCPToolInfo",
    "MCPToolResult",
    "MCPConfig",
    "MCPToolDefinition",
    "MCPConfigManager",
    "MCP_PRESETS",
    "MCP_TRANSPORT_STDIO",
    "MCP_TRANSPORT_SSE",
    "MCP_TRANSPORT_STREAMABLE_HTTP",
    "MCP_HTTP_TRANSPORTS",
    "normalize_mcp_transport",
    "detect_mcp_transport",
    "resolve_mcp_transport",
    "is_http_mcp_transport",
    "mcp_config_manager",
    "parse_mcp_tool_id",
    "format_mcp_tool_id",
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
    # utils
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
