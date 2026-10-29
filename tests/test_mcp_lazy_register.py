"""启动注册 MCP 时不连接。已保存的工具清单直接进注册表，调用时才连。

注册、注销之后把当时的全部工具交给向量同步。只交 MCP 工具会删掉其它点。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from pydantic_ai import RunContext
from pydantic_ai.tools import Tool
from pydantic_ai.usage import RunUsage
from pydantic_ai.models.test import TestModel

import gsuid_core.ai_core.mcp.startup as mcp_startup
from gsuid_core.ai_core.models import ToolBase, ToolContext
from gsuid_core.ai_core.register import _TOOL_REGISTRY
from gsuid_core.ai_core.mcp.client import MCPClient, MCPToolInfo, MCPToolResult
from gsuid_core.ai_core.mcp.startup import (
    unregister_mcp_server,
    register_all_mcp_tools,
    register_single_mcp_server,
)
from gsuid_core.ai_core.mcp.config_manager import MCPConfig, MCPToolDefinition

_MARKER = "unit_marker_keep"
_MARKER_CATEGORY = "unit_marker"


class _Configs:
    def __init__(self) -> None:
        self.items: list[tuple[str, MCPConfig]] = []

    def get_enabled_configs(self) -> list[tuple[str, MCPConfig]]:
        return [(cid, cfg) for cid, cfg in self.items if cfg.enabled]

    def get_config(self, config_id: str) -> MCPConfig | None:
        for cid, cfg in self.items:
            if cid == config_id:
                return cfg
        return None


async def _marker_fn(ctx: RunContext[ToolContext]) -> str:
    _ = ctx
    return "marker"


def _install_marker() -> None:
    _TOOL_REGISTRY[_MARKER_CATEGORY] = {
        _MARKER: ToolBase(
            name=_MARKER,
            description="keep this non-mcp tool in the sync map",
            plugin="core",
            tool=Tool(_marker_fn, takes_ctx=True),
            category=_MARKER_CATEGORY,
        )
    }


def _drop_marker() -> None:
    if _MARKER_CATEGORY in _TOOL_REGISTRY:
        del _TOOL_REGISTRY[_MARKER_CATEGORY]


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

    synced: list[set[str]] = []

    async def _capture_sync(tools_map: dict[str, ToolBase]) -> None:
        synced.append(set(tools_map))

    async def _run() -> None:
        holder.items = [("probe", MCPConfig(name="Probe", command="echo", tools=[_query_tool()]))]
        await register_all_mcp_tools()
        assert connected["list"] == 0
        assert synced
        assert "mcp_Probe_search" in synced[-1]
        assert _MARKER in synced[-1]
        tool = _TOOL_REGISTRY["mcp"]["mcp_Probe_search"]
        ctx: RunContext[ToolContext] = RunContext(deps=ToolContext(), model=TestModel(), usage=RunUsage())
        text = await tool.tool.function(ctx, query="ping")
        assert text == "ok"
        assert connected["call"] == 1

        holder.items = [("empty", MCPConfig(name="Empty", command="echo", tools=[]))]
        await register_all_mcp_tools()
        assert connected["list"] == 0
        assert len(synced) == 2
        assert "mcp_Empty_search" not in synced[-1]
        assert "mcp_Empty_search" not in _TOOL_REGISTRY["mcp"]
        assert _MARKER in synced[-1]

        holder.items = []
        await register_all_mcp_tools()
        assert connected["list"] == 0
        assert len(synced) == 3
        assert _MARKER in synced[-1]

    try:
        _install_marker()
        with (
            patch.object(MCPClient, "list_tools", _forbid_list),
            patch.object(MCPClient, "call_tool", _fake_call),
            patch.object(mcp_startup, "mcp_config_manager", holder),
            patch("gsuid_core.ai_core.rag.tools.sync_tools", _capture_sync),
        ):
            asyncio.run(_run())
    finally:
        _drop_marker()
        mcp_startup._mcp_clients.clear()
        if saved is None:
            if "mcp" in _TOOL_REGISTRY:
                del _TOOL_REGISTRY["mcp"]
        else:
            _TOOL_REGISTRY["mcp"] = saved


def test_single_register_and_unregister_sync_the_full_registry() -> None:
    saved: dict[str, ToolBase] | None = dict(_TOOL_REGISTRY["mcp"]) if "mcp" in _TOOL_REGISTRY else None
    connected = {"list": 0}
    holder = _Configs()
    synced: list[set[str]] = []

    async def _list_weather(self: MCPClient) -> list[MCPToolInfo]:
        connected["list"] += 1
        return [
            MCPToolInfo(
                name="weather",
                description="查询中国城市天气",
                input_schema={"type": "object", "properties": {}},
            )
        ]

    async def _capture_sync(tools_map: dict[str, ToolBase]) -> None:
        synced.append(set(tools_map))

    async def _run() -> None:
        holder.items = [("amp", MCPConfig(name="amp-weather", command="echo"))]
        count, message = await register_single_mcp_server("amp")
        assert count == 1
        assert "注册完成" in message
        assert connected["list"] == 1
        assert len(synced) == 1
        assert "mcp_amp-weather_weather" in synced[-1]
        assert _MARKER in synced[-1]
        assert "mcp_amp-weather_weather" in _TOOL_REGISTRY["mcp"]

        removed = await unregister_mcp_server("amp")
        assert removed == 1
        assert len(synced) == 2
        assert "mcp_amp-weather_weather" not in synced[-1]
        assert _MARKER in synced[-1]
        assert "mcp_amp-weather_weather" not in _TOOL_REGISTRY["mcp"]

    try:
        _install_marker()
        with (
            patch.object(MCPClient, "list_tools", _list_weather),
            patch.object(mcp_startup, "mcp_config_manager", holder),
            patch("gsuid_core.ai_core.rag.tools.sync_tools", _capture_sync),
        ):
            asyncio.run(_run())
    finally:
        _drop_marker()
        mcp_startup._mcp_clients.clear()
        if saved is None:
            if "mcp" in _TOOL_REGISTRY:
                del _TOOL_REGISTRY["mcp"]
        else:
            _TOOL_REGISTRY["mcp"] = saved
