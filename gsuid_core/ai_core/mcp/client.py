"""
MCP 客户端核心模块

提供通用的 MCP 客户端功能，用于连接和调用 MCP 服务器。
基于 fastmcp 实现，支持通过 stdio 和 sse 两种方式连接 MCP 服务器。

设计原则：
- 每次调用时建立连接、执行操作、断开连接（无状态模式）
- 支持通过代码配置连接参数（command, args, env）— stdio 模式
- 支持通过 URL 和请求头连接远程服务器（url, headers）— sse 模式
- 完全异步，兼容项目的 async 架构
"""

from typing import Any, Union
from dataclasses import field, dataclass

from fastmcp import Client
from fastmcp.client.transports import SSETransport, StdioTransport

from mcp.types import TextContent, ImageContent, ResourceLink, EmbeddedResource
from gsuid_core.i18n import t
from gsuid_core.logger import logger


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

    支持两种传输方式连接 MCP 服务器：
    - stdio: 通过 command + args + env 启动本地进程
    - sse: 通过 url + headers 连接远程 SSE 服务器

    每次操作独立建立连接，操作完成后自动断开。

    Args:
        name: MCP 服务器名称，用于日志标识
        command: 启动命令，如 "uvx", "npx", "python" 等（stdio 模式）
        args: 命令参数列表（stdio 模式）
        env: 环境变量字典（stdio 模式）
        url: SSE 服务器 URL（sse 模式）
        headers: HTTP 请求头字典（sse 模式，如 Authorization）

    Example (stdio):
        >>> client = MCPClient(
        ...     name="MiniMax",
        ...     command="uvx",
        ...     args=["minimax-coding-plan-mcp"],
        ...     env={"MINIMAX_API_KEY": "your_key"},
        ... )
        >>> tools = await client.list_tools()
        >>> result = await client.call_tool("web_search", {"query": "Python"})

    Example (sse):
        >>> client = MCPClient(
        ...     name="知乎搜索",
        ...     url="https://developer.zhihu.com/api/mcp/zhihu_search/v1/sse",
        ...     headers={"Authorization": "Bearer your_key"},
        ... )
        >>> tools = await client.list_tools()
        >>> result = await client.call_tool("zhihu_search", {"query": "RAG"})
    """

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""  # SSE 服务器 URL
    headers: dict[str, str] = field(default_factory=dict)  # SSE HTTP 请求头

    def _detect_transport(self) -> str:
        """根据 url / command 字段自动推断传输方式"""
        if self.url and isinstance(self.url, str) and self.url.startswith("http"):
            return "sse"
        return "stdio"

    def _create_transport(self) -> Union[StdioTransport, SSETransport]:
        """创建传输层（根据 url / command 自动选择 stdio 或 sse）"""
        transport_type = self._detect_transport()

        if transport_type == "sse":
            logger.debug(t("🔌 [MCP][{p0}] 使用 SSE 传输，URL: {p1}", p0=self.name, p1=self.url))
            return SSETransport(
                url=self.url,
                headers=self.headers if self.headers else None,
            )
        else:
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

        logger.info(t("🔌 [MCP][{p0}] 正在连接服务器并获取工具列表...", p0=self.name))

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

        logger.info(t("🔌 [MCP][{p0}] 获取到 {p1} 个工具", p0=self.name, p1=len(tools)))
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
                "🔌 [MCP][{p0}] 调用工具: {tool_name}, 参数: {truncated_args}",
                p0=self.name,
                tool_name=tool_name,
                truncated_args=truncated_args,
            )
        )

        async with client:
            result = await client.call_tool(
                name=tool_name,
                arguments=arguments or {},
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
                "🔌 [MCP][{p0}] 工具 {tool_name} 调用完成, is_error={p1}, 内容长度={p2}",
                p0=self.name,
                tool_name=tool_name,
                p1=tool_result.is_error,
                p2=len(tool_result.text),
            )
        )

        return tool_result
