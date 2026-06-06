"""
通用 MCP 工具调用模块

通过 MCP 配置 ID 和工具名称调用 MCP 服务器上的工具。
支持格式："{mcp_id} - {tool_name}"，例如 "minimax - web_search"
"""

from typing import Any

from gsuid_core.logger import logger
from gsuid_core.ai_core.mcp.client import MCPClient, MCPToolResult
from gsuid_core.ai_core.mcp.config_manager import parse_mcp_tool_id, mcp_config_manager


async def call_mcp_tool(
    mcp_tool_id: str,
    arguments: dict[str, Any],
) -> "MCPToolResult":
    """
    通用 MCP 工具调用函数

    Args:
        mcp_tool_id: MCP 工具 ID，格式为 "{mcp_id} - {tool_name}"
        arguments: 工具参数字典

    Returns:
        MCPToolResult

    Raises:
        ValueError: MCP 工具 ID 格式错误
        RuntimeError: MCP 配置或工具不存在
    """
    # 解析 MCP 工具 ID
    try:
        mcp_id, tool_name = parse_mcp_tool_id(mcp_tool_id)
    except ValueError as e:
        raise ValueError(f"无效的 MCP 工具 ID: {mcp_tool_id}, 错误: {e}")

    # 获取 MCP 配置
    config = mcp_config_manager.get_config(mcp_id)
    if not config:
        raise RuntimeError(f"MCP 配置 '{mcp_id}' 不存在，请检查配置")

    # 构建 MCP 客户端（自动根据 transport 选择 stdio / sse）
    client = MCPClient(
        name=config.name,
        command=config.command,
        args=config.args,
        env=config.env,
        url=config.url,
        headers=config.headers,
    )

    # 记录调用参数（截断过长的值）
    truncated_args = _truncate_args(arguments)
    logger.info(f"🔌 [MCP] 调用 {config.name}.{tool_name}, 参数: {truncated_args}")

    # 调用工具
    result = await client.call_tool(tool_name=tool_name, arguments=arguments)

    return result


def _truncate_args(args: dict[str, Any], max_len: int = 100) -> dict[str, Any]:
    """
    截断过长的参数值，避免日志被大段数据污染

    Args:
        args: 参数字典
        max_len: 单个值的最大显示长度

    Returns:
        截断后的参数字典副本
    """
    truncated: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > max_len:
            truncated[key] = f"{value[:max_len]}...[截断, 总长={len(value)}]"
        else:
            truncated[key] = value
    return truncated
