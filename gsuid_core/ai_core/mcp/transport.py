"""MCP 传输方式常量与规范化。

内部存储三种规范值:
- ``stdio``: 本地子进程
- ``sse``: 旧版 HTTP+SSE（兼容存量远程服务）
- ``streamable_http``: Streamable HTTP（当前推荐的远程传输）

导入/粘贴配置里常见的 ``http`` / ``streamable-http`` 会归一到 ``streamable_http``。
"""

MCP_TRANSPORT_STDIO = "stdio"
MCP_TRANSPORT_SSE = "sse"
MCP_TRANSPORT_STREAMABLE_HTTP = "streamable_http"

MCP_HTTP_TRANSPORTS = frozenset({MCP_TRANSPORT_SSE, MCP_TRANSPORT_STREAMABLE_HTTP})
MCP_CANONICAL_TRANSPORTS = frozenset(
    {
        MCP_TRANSPORT_STDIO,
        MCP_TRANSPORT_SSE,
        MCP_TRANSPORT_STREAMABLE_HTTP,
    }
)

_TRANSPORT_ALIASES = {
    "http": MCP_TRANSPORT_STREAMABLE_HTTP,
    "streamablehttp": MCP_TRANSPORT_STREAMABLE_HTTP,
}


def normalize_mcp_transport(raw: str) -> str:
    """将用户/导入配置中的传输名规范为内部值。

    空字符串或 ``auto`` 返回空串，交给 :func:`detect_mcp_transport`。
    未知名称也返回空串，避免把脏值写进配置。
    """
    value = (raw or "").strip()
    if not value or value == "auto":
        return ""
    lowered = value.lower().replace("-", "_")
    if lowered in MCP_CANONICAL_TRANSPORTS:
        return lowered
    if lowered in _TRANSPORT_ALIASES:
        return _TRANSPORT_ALIASES[lowered]
    return ""


def detect_mcp_transport(*, url: str = "", command: str = "") -> str:
    """根据 url / command 推断传输方式。

    - URL 路径以 ``/sse`` 结尾 → ``sse``（旧远程端点）
    - 其它 http(s) URL → ``streamable_http``
    - 否则 → ``stdio``
    """
    _ = command
    if url and isinstance(url, str) and url.startswith("http"):
        path = url.split("?", 1)[0].rstrip("/")
        if path.endswith("/sse"):
            return MCP_TRANSPORT_SSE
        return MCP_TRANSPORT_STREAMABLE_HTTP
    return MCP_TRANSPORT_STDIO


def resolve_mcp_transport(raw: str = "", *, url: str = "", command: str = "") -> str:
    """显式 transport 优先，否则按 url/command 推断。"""
    normalized = normalize_mcp_transport(raw)
    if normalized:
        return normalized
    return detect_mcp_transport(url=url, command=command)


def is_http_mcp_transport(transport: str) -> bool:
    """是否为远程 HTTP 类传输（sse 或 streamable_http）。"""
    return transport in MCP_HTTP_TRANSPORTS
