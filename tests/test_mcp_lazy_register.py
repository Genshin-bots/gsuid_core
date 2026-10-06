"""启动注册 MCP 时不连接。已保存的工具清单直接进注册表，调用时才连。"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from pydantic_ai import RunContext
from pydantic_ai.usage import RunUsage
from pydantic_ai.models.test import TestModel

import gsuid_core.ai_core.mcp.startup as mcp_startup
from gsuid_core.ai_core.models import ToolBase, ToolContext
from gsuid_core.ai_core.register import _TOOL_REGISTRY
from gsuid_core.ai_core.mcp.client import MCPClient, MCPToolInfo, MCPToolResult
from gsuid_core.ai_core.mcp.startup import register_all_mcp_tools
from gsuid_core.ai_core.mcp.config_manager import MCPConfig, MCPToolDefinition


class _Configs:
    def __init__(self) -> None:
        self.items: list[tuple[str, MCPConfig]] = []

    def get_enabled_configs(self) -> list[tuple[str, MCPConfig]]:
        return list(self.items)


def _query_tool() -> MCPToolDefinition:
    tool = MCPToolDefinition(name="search", description="look up")
    query: dict[str, str | bool] = {"type": "string", "description": "Query", "required": True}
    tool.parameters["query"] = query
    return tool


def test_startup_registers_saved_catalog_without_connecting() -> None:
    saved: dict[str, ToolBase] | None = dict(_TOOL_REGISTRY["mcp"]) if "mcp" in _TOOL_REGISTRY else None
    connected = {"list": 0, "call": 0}
    holder = _Configs()

    async def _forbid_list(self: MCPClient) -> list[MCPToolInfo]:
        connected["list"] += 1
        raise AssertionError("startup listed tools")

    async def _fake_call(
        self: MCPClient,
        tool_name: str,
        arguments: dict[str, object] | None = None,
    ) -> MCPToolResult:
        connected["call"] += 1
        assert tool_name == "search"
        assert arguments == {"query": "ping"}
        return MCPToolResult(content=[{"type": "text", "text": "ok"}])

    async def _run() -> None:
        holder.items = [("probe", MCPConfig(name="Probe", command="echo", tools=[_query_tool()]))]
        await register_all_mcp_tools()
        assert connected["list"] == 0
        tool = _TOOL_REGISTRY["mcp"]["mcp_Probe_search"]
        ctx: RunContext[ToolContext] = RunContext(deps=ToolContext(), model=TestModel(), usage=RunUsage())
        text = await tool.tool.function(ctx, query="ping")
        assert text == "ok"
        assert connected["call"] == 1

        holder.items = [("empty", MCPConfig(name="Empty", command="echo", tools=[]))]
        await register_all_mcp_tools()
        assert connected["list"] == 0
        assert "mcp_Empty_search" not in _TOOL_REGISTRY["mcp"]

    try:
        with (
            patch.object(MCPClient, "list_tools", _forbid_list),
            patch.object(MCPClient, "call_tool", _fake_call),
            patch.object(mcp_startup, "mcp_config_manager", holder),
        ):
            asyncio.run(_run())
    finally:
        mcp_startup._mcp_clients.clear()
        if saved is None:
            if "mcp" in _TOOL_REGISTRY:
                del _TOOL_REGISTRY["mcp"]
        else:
            _TOOL_REGISTRY["mcp"] = saved
