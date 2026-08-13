from pathlib import Path

from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport

from gsuid_core.ai_core.mcp.client import MCPClient
from gsuid_core.ai_core.mcp.transport import (
    MCP_TRANSPORT_SSE,
    MCP_TRANSPORT_STDIO,
    MCP_TRANSPORT_STREAMABLE_HTTP,
    detect_mcp_transport,
    is_http_mcp_transport,
    resolve_mcp_transport,
    normalize_mcp_transport,
)
from gsuid_core.ai_core.mcp.config_manager import MCPConfig, MCPConfigManager


def test_normalize_aliases_to_streamable_http():
    assert normalize_mcp_transport("http") == MCP_TRANSPORT_STREAMABLE_HTTP
    assert normalize_mcp_transport("streamable-http") == MCP_TRANSPORT_STREAMABLE_HTTP
    assert normalize_mcp_transport("streamableHttp") == MCP_TRANSPORT_STREAMABLE_HTTP
    assert normalize_mcp_transport("STREAMABLE_HTTP") == MCP_TRANSPORT_STREAMABLE_HTTP


def test_normalize_canonical_and_empty():
    assert normalize_mcp_transport("stdio") == MCP_TRANSPORT_STDIO
    assert normalize_mcp_transport("sse") == MCP_TRANSPORT_SSE
    assert normalize_mcp_transport("") == ""
    assert normalize_mcp_transport("auto") == ""
    assert normalize_mcp_transport("not-a-transport") == ""


def test_detect_url_suffix_and_default():
    assert detect_mcp_transport(url="https://example.com/api/mcp/sse") == MCP_TRANSPORT_SSE
    assert detect_mcp_transport(url="https://example.com/sse?x=1") == MCP_TRANSPORT_SSE
    assert detect_mcp_transport(url="https://example.com/mcp") == MCP_TRANSPORT_STREAMABLE_HTTP
    assert detect_mcp_transport(url="", command="uvx") == MCP_TRANSPORT_STDIO


def test_resolve_explicit_wins_over_url():
    assert resolve_mcp_transport("sse", url="https://example.com/mcp") == MCP_TRANSPORT_SSE
    assert resolve_mcp_transport("http", url="https://example.com/sse") == MCP_TRANSPORT_STREAMABLE_HTTP


def test_is_http_transport():
    assert is_http_mcp_transport(MCP_TRANSPORT_SSE)
    assert is_http_mcp_transport(MCP_TRANSPORT_STREAMABLE_HTTP)
    assert not is_http_mcp_transport(MCP_TRANSPORT_STDIO)


def test_config_from_dict_http_alias():
    cfg = MCPConfig.from_dict(
        {
            "name": "Example",
            "transport": "http",
            "url": "https://example.com/mcp",
        }
    )
    assert cfg.get_transport() == MCP_TRANSPORT_STREAMABLE_HTTP
    dumped = cfg.to_dict()
    assert dumped["transport"] == MCP_TRANSPORT_STREAMABLE_HTTP
    assert dumped["url"] == "https://example.com/mcp"


def test_config_from_dict_legacy_sse_url():
    cfg = MCPConfig.from_dict(
        {
            "name": "Zhihu",
            "url": "https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse",
        }
    )
    assert cfg.get_transport() == MCP_TRANSPORT_SSE


def test_client_creates_matching_transport():
    http_client = MCPClient(
        name="http",
        transport="streamable_http",
        url="https://example.com/mcp",
    )
    assert isinstance(http_client._create_transport(), StreamableHttpTransport)

    sse_client = MCPClient(
        name="sse",
        transport="sse",
        url="https://example.com/sse",
    )
    assert isinstance(sse_client._create_transport(), SSETransport)

    auto_http = MCPClient(name="auto", url="https://example.com/mcp")
    assert isinstance(auto_http._create_transport(), StreamableHttpTransport)

    auto_sse = MCPClient(name="auto-sse", url="https://example.com/v1/sse")
    assert isinstance(auto_sse._create_transport(), SSETransport)

    stdio_client = MCPClient(name="local", command="uvx", args=["demo"])
    assert isinstance(stdio_client._create_transport(), StdioTransport)


def test_manager_update_can_switch_to_streamable_http(tmp_path: Path):
    mgr = MCPConfigManager.__new__(MCPConfigManager)
    mgr._base_path = tmp_path
    mgr._cache = {}

    ok, msg = mgr.create_config(
        "demo",
        MCPConfig(name="Demo", transport="stdio", command="uvx"),
    )
    assert ok, msg

    ok, msg = mgr.update_config(
        "demo",
        {
            "transport": "streamable_http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer x"},
            "command": "",
        },
    )
    assert ok, msg

    got = mgr.get_config("demo")
    assert got is not None
    assert got.get_transport() == MCP_TRANSPORT_STREAMABLE_HTTP
    assert got.url == "https://example.com/mcp"
    assert got.headers["Authorization"] == "Bearer x"
