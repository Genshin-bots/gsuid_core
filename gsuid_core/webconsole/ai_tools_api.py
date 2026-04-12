"""
AI Tools APIs

提供 AI 工具管理相关的 RESTful APIs，包括获取工具列表等。
"""

from typing import Dict, List

from fastapi import Depends

from gsuid_core.ai_core.register import get_all_tools
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


@app.get("/api/ai/tools/list")
async def get_ai_tools_list(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取所有已注册的 AI 工具列表（按插件分组）

    Returns:
        status: 0成功，1失败
        data: 包含 tools 字典（按插件名称分组）和总 count
    """
    tools_map = get_all_tools()

    # 按插件名称分组
    tools_by_plugin: Dict[str, List[Dict]] = {}
    for name, tool_base in tools_map.items():
        plugin_name = tool_base.plugin
        if plugin_name not in tools_by_plugin:
            tools_by_plugin[plugin_name] = []
        tools_by_plugin[plugin_name].append(
            {
                "name": tool_base.name,
                "description": tool_base.description,
            }
        )

    # 每个插件内的工具按名称排序
    for plugin_name in tools_by_plugin:
        tools_by_plugin[plugin_name].sort(key=lambda x: x["name"])

    total_count = len(tools_map)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "tools": tools_by_plugin,
            "count": total_count,
        },
    }


@app.get("/api/ai/tools/{tool_name}")
async def get_ai_tool_detail(tool_name: str, _: Dict = Depends(require_auth)) -> Dict:
    """
    获取指定 AI 工具的详细信息

    Args:
        tool_name: 工具名称

    Returns:
        status: 0成功，1失败
        data: 工具详情
    """
    tools_map = get_all_tools()
    tool_base = tools_map.get(tool_name)

    if tool_base is None:
        return {
            "status": 1,
            "msg": f"Tool '{tool_name}' not found",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "name": tool_base.name,
            "description": tool_base.description,
            "plugin": tool_base.plugin,
        },
    }
