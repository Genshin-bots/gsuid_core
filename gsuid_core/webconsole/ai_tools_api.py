"""
AI Tools APIs

提供 AI 工具管理相关的 RESTful APIs，包括获取工具列表等。
"""

from typing import Any, Dict, List, Optional

from fastapi import Query, Depends
from pydantic import BaseModel

from gsuid_core.ai_core.register import get_all_tools, find_tool_base, get_registered_tools
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.webconsole._local_test_gate import require_auth_or_local_test

from ._api_tags import AI_TOOLS


@app.get("/api/ai/tools/list", summary="获取 AI 工具列表", tags=AI_TOOLS)
async def get_ai_tools_list(
    _: Dict[str, Any] = Depends(require_auth),
    category: Optional[str] = Query(None, description="按分类筛选，如 'self', 'buildin', 'default', 'common'"),
    plugin: Optional[str] = Query(None, description="按插件名称筛选，如 'core', 'GenshinUID'"),
) -> Dict[str, Any]:
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
    tools_list: List[Dict[str, Any]] = []
    by_category: Dict[str, List[Dict[str, Any]]] = {}
    by_plugin: Dict[str, List[Dict[str, Any]]] = {}
    categories_set: set[str] = set()
    plugins_set: set[str] = set()

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


@app.get("/api/ai/tools/categories", summary="获取工具分类列表", tags=AI_TOOLS)
async def get_ai_tools_categories(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
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


@app.get("/api/ai/tools/{tool_name}", summary="获取指定工具详情", tags=AI_TOOLS)
async def get_ai_tool_detail(tool_name: str, _: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
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


class AssemblePreviewRequest(BaseModel):
    """工具装配预览请求（工具选择评测用）。"""

    query: str = ""


@app.post(
    "/api/ai/tools/assemble_preview",
    summary="预览某条 query 会装配出哪些工具",
    tags=AI_TOOLS,
)
async def assemble_tools_preview(
    body: AssemblePreviewRequest,
    _auth: Any = Depends(require_auth_or_local_test),
) -> Dict[str, Any]:
    """跑真实的附加池装配链路（向量召回 → 能力族展开），返回本轮会给模型的工具。

    供控制台诊断与 `eval/tool_selection` 量化 Pool Recall。
    鉴权：webconsole Bearer **或** local-test 模式。
    """
    from gsuid_core.ai_core.rag.tools import (
        get_main_agent_tools,
        expand_tools_to_families,
        search_tools_with_entity_routing,
    )
    from gsuid_core.ai_core.configs.ai_config import ai_config

    recall: int = ai_config.get_config("tool_search_recall").data
    max_extra: int = ai_config.get_config("tool_extra_pool_max").data

    core_tools = await get_main_agent_tools()
    core_names = {t.name for t in core_tools}

    # 必须与 gs_agent 走同一条装配路径（含 L0 实体路由），否则评测测的不是生产行为。
    # 单轮预览没有历史，route_text 与 query 同为本条消息。
    seeds = await search_tools_with_entity_routing(
        query=body.query,
        route_text=body.query,
        limit=recall,
        non_category=["self", "buildin"],
    )
    pool = expand_tools_to_families(seeds, exclude_names=core_names, max_tools=max_extra)

    def _plugin_of(name: str) -> str:
        tb = find_tool_base(name)
        if tb is None:
            return "unknown"
        return tb.plugin

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "query": body.query,
            "seeds": [{"name": t.name, "plugin": _plugin_of(t.name)} for t in seeds],
            "pool": [{"name": t.name, "plugin": _plugin_of(t.name)} for t in pool],
            "core_pool_size": len(core_names),
            "recall": recall,
            "max_extra": max_extra,
        },
    }


# 路径**不能**放在 /api/ai/tools/ 下：会被更早注册的 GET /api/ai/tools/{tool_name}
# 抢先匹配（把 entity_index 当成工具名）。
@app.get(
    "/api/ai/entity_index",
    summary="导出实体身份索引",
    tags=AI_TOOLS,
)
async def dump_entity_index(
    _auth: Any = Depends(require_auth_or_local_test),
) -> Dict[str, Any]:
    """导出 surface → 插件的实体身份索引。

    控制台诊断与评测 ground truth 共用。鉴权：webconsole Bearer **或** local-test。
    """
    from gsuid_core.ai_core.entity_index import get_entity_index

    entries = [
        {"surface": surface, "plugins": ref.plugins, "ambiguous": ref.is_ambiguous}
        for surface, ref in get_entity_index().items()
    ]
    return {"status": 0, "msg": "ok", "data": {"count": len(entries), "entries": entries}}
