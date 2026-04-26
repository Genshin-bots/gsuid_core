"""
AI Tools APIs

提供 AI 工具管理相关的 RESTful APIs，包括获取工具列表等。
"""

from typing import Dict, List, Optional

from fastapi import Query, Depends

from gsuid_core.ai_core.register import get_all_tools, get_registered_tools
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


@app.get("/api/ai/tools/list")
async def get_ai_tools_list(
    _: Dict = Depends(require_auth),
    category: Optional[str] = Query(None, description="按分类筛选，如 'self', 'buildin', 'default', 'common'"),
    plugin: Optional[str] = Query(None, description="按插件名称筛选，如 'core', 'GenshinUID'"),
) -> Dict:
    """
    获取所有已注册的 AI 工具列表

    **支持按分类和插件筛选**

    Returns:
        status: 0成功，1失败
        data: 包含以下结构：
        - tools: 工具列表（每个工具包含 name, description, plugin, category）
        - by_category: 按分类分组的工具字典
        - by_plugin: 按插件分组的工具字典
        - categories: 所有分类列表
        - plugins: 所有插件列表
        - count: 工具总数
    """
    # 获取按分类组织的工具
    tools_by_category = get_registered_tools()

    # 构建完整工具列表（包含分类和插件信息）
    tools_list: List[Dict] = []
    by_category: Dict[str, List[Dict]] = {}
    by_plugin: Dict[str, List[Dict]] = {}
    categories_set: set = set()
    plugins_set: set = set()

    for cat_name, cat_tools in tools_by_category.items():
        by_category[cat_name] = []
        categories_set.add(cat_name)

        for tool_name, tool_base in cat_tools.items():
            plugin_name = tool_base.plugin
            plugins_set.add(plugin_name)

            tool_info = {
                "name": tool_base.name,
                "description": tool_base.description,
                "plugin": plugin_name,
                "category": cat_name,
            }

            tools_list.append(tool_info)
            by_category[cat_name].append(tool_info)

            if plugin_name not in by_plugin:
                by_plugin[plugin_name] = []
            by_plugin[plugin_name].append(tool_info)

    # 按名称排序
    for cat_name in by_category:
        by_category[cat_name].sort(key=lambda x: x["name"])
    for plugin_name in by_plugin:
        by_plugin[plugin_name].sort(key=lambda x: x["name"])
    tools_list.sort(key=lambda x: x["name"])

    # 应用筛选
    filtered_tools = tools_list
    if category:
        filtered_tools = [t for t in filtered_tools if t["category"] == category]
    if plugin:
        filtered_tools = [t for t in filtered_tools if t["plugin"] == plugin]

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "tools": filtered_tools,
            "by_category": by_category,
            "by_plugin": by_plugin,
            "categories": sorted(list(categories_set)),
            "plugins": sorted(list(plugins_set)),
            "count": len(filtered_tools),
            "total_count": len(tools_list),
        },
    }


@app.get("/api/ai/tools/categories")
async def get_ai_tools_categories(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取所有工具分类列表

    Returns:
        status: 0成功
        data: 分类列表，每个分类包含名称和工具数量
    """
    tools_by_category = get_registered_tools()

    categories = []
    for cat_name, cat_tools in tools_by_category.items():
        categories.append(
            {
                "name": cat_name,
                "count": len(cat_tools),
            }
        )

    categories.sort(key=lambda x: x["name"])

    return {
        "status": 0,
        "msg": "ok",
        "data": categories,
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

    # 获取工具所属的分类
    tools_by_category = get_registered_tools()
    tool_category = None
    for cat_name, cat_tools in tools_by_category.items():
        if tool_name in cat_tools:
            tool_category = cat_name
            break

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "name": tool_base.name,
            "description": tool_base.description,
            "plugin": tool_base.plugin,
            "category": tool_category,
        },
    }
