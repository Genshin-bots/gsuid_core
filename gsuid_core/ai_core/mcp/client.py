"""
MCP 客户端核心模块

提供通用的 MCP 客户端功能，用于连接和调用 MCP 服务器。
基于 fastmcp 实现，支持 stdio / sse / Streamable HTTP 三种传输。

设计原则：
- 每次调用时建立连接、执行操作、断开连接（无状态模式）
- 支持通过代码配置连接参数（command, args, env）— stdio 模式
- 支持通过 URL 和请求头连接远程服务器（url, headers）— sse / streamable_http
- 完全异步，兼容项目的 async 架构
"""

from typing import Any, Union
from dataclasses import field, dataclass

from fastmcp import Client
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport

from mcp.types import TextContent, ImageContent, ResourceLink, EmbeddedResource
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.mcp.transport import (
    MCP_TRANSPORT_SSE,
    MCP_TRANSPORT_STDIO,
    MCP_TRANSPORT_STREAMABLE_HTTP,
    resolve_mcp_transport,
)


@dataclass
class MCPToolInfo:
    """MCP 工具信息"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class MCPToolResult:
    """MCP 工具调用结果"""

    content: list[dict[str, Any]]
    is_error: bool = False

    @property
    def text(self) -> str:
        """提取所有文本内容并拼接"""
        texts: list[str] = []
        for item in self.content:
            if "type" in item and item["type"] == "text" and "text" in item:
                text_value = item["text"]
                if isinstance(text_value, str):
                    texts.append(text_value)
        return "\n".join(texts)


@dataclass
class MCPClient:
    """
    MCP 客户端

    支持三种传输方式连接 MCP 服务器：
    - stdio: 通过 command + args + env 启动本地进程
    - sse: 通过 url + headers 连接远程 SSE 服务器（旧传输）
    - streamable_http: 通过 url + headers 连接远程 Streamable HTTP 服务器

    每次操作独立建立连接，操作完成后自动断开。

    Args:
        name: MCP 服务器名称，用于日志标识
        command: 启动命令，如 "uvx", "npx", "python" 等（stdio 模式）
        args: 命令参数列表（stdio 模式）
        env: 环境变量字典（stdio 模式）
        url: 远程服务器 URL（sse / streamable_http 模式）
        headers: HTTP 请求头字典（远程模式，如 Authorization）
        transport: 显式传输方式；空则按 url / command 推断

    Example (stdio):
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

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = ""

    def _resolve_transport(self) -> str:
        """显式 transport 优先，否则按 url / command 推断。"""
        return resolve_mcp_transport(
            self.transport,
            url=self.url,
            command=self.command,
        )

    def _create_transport(
        self,
    ) -> Union[StdioTransport, SSETransport, StreamableHttpTransport]:
        """创建传输层（stdio / sse / streamable_http）。"""
        transport_type = self._resolve_transport()

        if transport_type == MCP_TRANSPORT_SSE:
            logger.debug(t("log.mcp.sse_transport_url", p0=self.name, p1=self.url))
            return SSETransport(
                url=self.url,
                headers=self.headers if self.headers else None,
            )
        if transport_type == MCP_TRANSPORT_STREAMABLE_HTTP:
            logger.debug(t("log.mcp.streamable_http_transport_url", p0=self.name, p1=self.url))
            return StreamableHttpTransport(
                url=self.url,
                headers=self.headers if self.headers else None,
            )
        if transport_type != MCP_TRANSPORT_STDIO:
            logger.warning(
                t(
                    "log.mcp.unknown_transport_fallback_stdio",
                    p0=self.name,
                    p1=transport_type,
                )
            )
        return StdioTransport(
            command=self.command,
            args=self.args,
            env=self.env if self.env else None,
        )

    @staticmethod
    def _truncate_args(arguments: dict[str, Any] | None, max_len: int = 100) -> dict[str, Any]:
        """
        截断参数中的长字符串值，避免 base64 等大段数据污染日志

        Args:
            arguments: 工具调用参数字典
            max_len: 单个值的最大显示长度

        Returns:
            截断后的参数字典副本
        """
        if not arguments:
            return {}
        truncated: dict[str, Any] = {}
        for key, value in arguments.items():
            if isinstance(value, str) and len(value) > max_len:
                truncated[key] = f"{value[:max_len]}...[截断, 总长={len(value)}]"
            else:
                truncated[key] = value
        return truncated

    async def list_tools(self) -> list[MCPToolInfo]:
        """
        列出 MCP 服务器提供的所有工具

        Returns:
            工具信息列表

        Raises:
            连接或通信失败时抛出异常
        """
        transport = self._create_transport()
        client = Client(transport)

        logger.info(t("log.mcp.connecting_server_fetching_list", p0=self.name))

        async with client:
            raw_tools = await client.list_tools()

        tools: list[MCPToolInfo] = []
        for tool in raw_tools:
            schema = tool.inputSchema
            tools.append(
                MCPToolInfo(
                    name=tool.name,
                    description=tool.description if tool.description else "",
                    input_schema=schema if schema else {},
                )
            )

        logger.info(t("log.mcp.tool_fetched_tools", p0=self.name, p1=len(tools)))
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> MCPToolResult:
        """
        调用 MCP 服务器上的指定工具

        Args:
            tool_name: 工具名称
            arguments: 工具参数字典

        Returns:
            工具调用结果

        Raises:
            连接或调用失败时抛出异常
        """
        transport = self._create_transport()
        client = Client(transport)

        # 截断过长的参数值，避免 base64 等大段数据污染日志
        truncated_args = self._truncate_args(arguments)
        logger.info(
            t(
                "log.mcp.calling_name_args_truncated_2",
                p0=self.name,
                tool_name=tool_name,
                truncated_args=truncated_args,
            )
        )

        async with client:
            # FastMCP 3 默认 raise_on_error=True，会把工具错误直接抛成 ToolError；
            # 本项目统一用 MCPToolResult.is_error 处理失败路径，故显式关闭。
            result = await client.call_tool(
                name=tool_name,
                arguments=arguments or {},
                raise_on_error=False,
            )

        # 将 CallToolResult 转换为统一格式
        content_list: list[dict[str, Any]] = []
        for item in result.content:
            if isinstance(item, TextContent):
                content_list.append({"type": "text", "text": item.text})
            elif isinstance(item, ImageContent):
                content_list.append(
                    {
                        "type": "image",
                        "data": item.data,
                        "mimeType": item.mimeType,
                    }
                )
            elif isinstance(item, (ResourceLink, EmbeddedResource)):
                content_list.append({"type": "resource", "text": str(item)})
            else:
                content_list.append({"type": "text", "text": str(item)})

        tool_result = MCPToolResult(
            content=content_list,
            is_error=result.is_error,
        )

        logger.info(
            t(
                "log.mcp.name_call_fail_content",
                p0=self.name,
                tool_name=tool_name,
                p1=tool_result.is_error,
                p2=len(tool_result.text),
            )
        )

        return tool_result
