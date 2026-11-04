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

import re
from typing import Any, Union
from dataclasses import field, dataclass

from fastmcp import Client
from mcp.types import TextContent, ImageContent, ResourceLink, EmbeddedResource
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.mcp.transport import (
    MCP_TRANSPORT_SSE,
    MCP_TRANSPORT_STDIO,
    MCP_TRANSPORT_STREAMABLE_HTTP,
    resolve_mcp_transport,
)

# GsCore 内部资源句柄：img_ = RM 内存图片（16 位 hex），res_ = Kanban artifact（12 位 hex）。
# 两者都只存在于主进程（RM 是内存 dict，artifact 落在主进程的 DB / 磁盘），stdio 的
# MCP 子进程取不到——直接透传会被当成文件路径去找，必然回一句「图片文件不存在」。
_RESOURCE_HANDLE_RE = re.compile(r"^(?:img_[0-9a-f]{16}|res_[0-9a-f]{12})(?:\.(?:png|jpe?g|webp|gif))?$")


async def _materialize_resource_handle(value: str) -> str:
    """把 GsCore 资源句柄解析成 data URI，供 stdio 的 MCP 子进程直接消费。

    ``img_``（RM 内存图片）与 ``res_``（Kanban artifact）都只存在于主进程，子进程
    取不到；原样透传会被当成文件路径去找，下游只会回一句「图片文件不存在」。

    Args:
        value: 单个参数值

    Returns:
        解析后的 data URI；不是资源句柄、或句柄取不出来时原样返回
    """
    if not _RESOURCE_HANDLE_RE.match(value):
        return value
    # Agent 会给句柄补图片扩展名，RM / artifact 里存的是裸句柄
    handle = re.sub(r"\.(?:png|jpe?g|webp|gif)$", "", value)

    # 延迟导入：image_reader 依赖 register / content_guard，模块级引入会形成导入环
    from gsuid_core.ai_core.buildin_tools.image_reader import _resolve_image_to_url

    uri, err = await _resolve_image_to_url(handle)
    if uri is None:
        # 取不到就原样放行：句柄也可能是外部系统的同形态 ID，改抛错会打断原本能用的调用。
        # 错误文案可能多行（如非图片 artifact 的处置建议），压成单行并截断再进日志。
        brief = " ".join((err or "未知原因").split())[:200]
        logger.warning(t("log.mcp.resource_handle_unresolved", handle=handle, e=brief))
        return value
    return uri


async def _materialize_resource_args(value: Any) -> Any:
    """递归把参数里的 GsCore 资源句柄换成 data URI（list / dict 里的同样处理）。"""
    if isinstance(value, str):
        return await _materialize_resource_handle(value)
    if isinstance(value, list):
        return [await _materialize_resource_args(item) for item in value]
    if isinstance(value, dict):
        return {key: await _materialize_resource_args(item) for key, item in value.items()}
    return value


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

        # img_ / res_ 只在本进程可达，stdio 子进程拿不到 → 发送前物化成 data URI。
        # 远程传输（sse / streamable_http）对面可能就是 GsCore 自己的 MCP Server，
        # 它认句柄（server.py 用 RM.peek 取），提前物化反而会打断，故只处理 stdio。
        if isinstance(transport, StdioTransport):
            arguments = await _materialize_resource_args(arguments)

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
