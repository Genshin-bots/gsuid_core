"""
AI Tools APIs

提供 AI 工具管理相关的 RESTful APIs，包括获取工具列表等。
"""

from enum import Enum
from typing import Any, Dict, List, Optional, TypedDict, NamedTuple

from fastapi import Query, Depends
from pydantic import BaseModel

from gsuid_core.ai_core.register import get_all_tools, find_tool_base, get_registered_tools
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.webconsole._local_test_gate import require_auth_or_local_test
from gsuid_core.ai_core.buildin_tools.dynamic_tool_discovery import get_capability_gaps

from ._api_tags import AI_TOOLS


class ToolDiagnosticFilter(str, Enum):
    """工具诊断筛选（控制台「有问题 / 空描述」下拉）。"""

    all = "all"
    issues = "issues"
    empty_description = "empty_description"


class ToolListItem(TypedDict):
    """工具列表条目。"""

    name: str
    description: str
    plugin: str
    category: str


class ToolDiagnostic(NamedTuple):
    """工具诊断结果。"""

    level: str
    empty_description: bool
    meta: bool


def _diagnose_tool(name: str, description: str, category: str) -> ToolDiagnostic:
    """工具诊断。

    与前端 ``featureUtils.diagnoseTool`` **同语义**——两边不一致时，下拉里的数字
    会和筛选后的条数对不上。level：error=空描述或空名；warn=meta 分类（不可向量
    检索）；ok=其余。
    """
    empty_description = not (description or "").strip()
    if empty_description or not (name or "").strip():
        return ToolDiagnostic("error", empty_description, False)
    if category == "meta":
        return ToolDiagnostic("warn", False, True)
    return ToolDiagnostic("ok", False, False)


def _tool_matches(
    tool: ToolListItem,
    *,
    want_category: Optional[str],
    want_plugin: Optional[str],
    keyword: str,
    diagnostic: ToolDiagnosticFilter,
) -> bool:
    """工具是否命中筛选条件。keyword 需已小写。"""
    if want_category is not None and tool["category"] != want_category:
        return False
    if want_plugin is not None and tool["plugin"] != want_plugin:
        return False
    if keyword:
        if keyword not in tool["name"].lower() and keyword not in tool["description"].lower():
            return False
    if diagnostic is not ToolDiagnosticFilter.all:
        diag = _diagnose_tool(tool["name"], tool["description"], tool["category"])
        if diagnostic is ToolDiagnosticFilter.empty_description and not diag.empty_description:
            return False
        if diagnostic is ToolDiagnosticFilter.issues and diag.level == "ok":
            return False
    return True


@app.get("/api/ai/tools/list", summary="获取 AI 工具列表", tags=AI_TOOLS)
async def get_ai_tools_list(
    _: Dict[str, Any] = Depends(require_auth),
    category: Optional[str] = Query(None, description="按分类筛选，如 'self', 'buildin', 'default', 'common'"),
    plugin: Optional[str] = Query(None, description="按插件名称筛选，如 'core', 'GenshinUID'"),
    search: Optional[str] = Query(None, description="按工具名或描述模糊匹配"),
    diagnostic: ToolDiagnosticFilter = Query(
        ToolDiagnosticFilter.all, description="诊断筛选：all / issues / empty_description"
    ),
    limit: Optional[int] = Query(None, ge=1, le=2000, description="单页条数；不传=全量"),
    offset: int = Query(0, ge=0, description="偏移量"),
) -> Dict[str, Any]:
    """
    获取所有已注册的 AI 工具列表

    **支持按分类、插件、搜索、诊断筛选**

    筛选在服务端完成后才切片分页，翻页不会漏条目。分面计数（分类 / 插件 / 诊断）
    统计的是**总数**，与 `limit`/`offset` 无关——下拉里的数字不会因为翻页而变。

    Returns:
        status: 0成功，1失败
        data: 包含以下结构：
        - tools: 当前页工具列表（每个工具包含 name, description, plugin, category）
        - by_category: 按分类分组的工具字典
        - by_plugin: 按插件分组的工具字典
        - categories: 所有分类列表
        - plugins: 所有插件列表
        - count: 命中筛选的工具总数
        - total_count: 工具总数（筛选前）
        - category_counts / plugin_counts: 分面总数
        - diagnostic_counts: 诊断概览
        - limit / offset / has_more: 分页信息
    """
    # 获取按分类组织的工具
    tools_by_category = get_registered_tools()

    # 构建完整工具列表（包含分类和插件信息）
    all_tools: List[ToolListItem] = []
    by_category: Dict[str, List[ToolListItem]] = {}
    by_plugin: Dict[str, List[ToolListItem]] = {}
    categories_set: set[str] = set()
    plugins_set: set[str] = set()

    for cat_name, cat_tools in tools_by_category.items():
        by_category[cat_name] = []
        categories_set.add(cat_name)

        for tool_name, tool_base in cat_tools.items():
            plugin_name = tool_base.plugin
            plugins_set.add(plugin_name)

            tool_info: ToolListItem = {
                "name": tool_base.name,
                "description": tool_base.description,
                "plugin": plugin_name,
                "category": cat_name,
            }

            all_tools.append(tool_info)
            by_category[cat_name].append(tool_info)
            by_plugin.setdefault(plugin_name, []).append(tool_info)

    # 按名称排序
    for cat_name in by_category:
        by_category[cat_name].sort(key=lambda x: x["name"])
    for plugin_name in by_plugin:
        by_plugin[plugin_name].sort(key=lambda x: x["name"])
    all_tools.sort(key=lambda x: x["name"])

    keyword = (search or "").strip().lower()

    # 先筛后切：命中全部条件的集合，分页从这里切
    filtered = [
        tool
        for tool in all_tools
        if _tool_matches(
            tool,
            want_category=category,
            want_plugin=plugin,
            keyword=keyword,
            diagnostic=diagnostic,
        )
    ]
    total = len(filtered)
    page_items = filtered[offset:] if limit is None else filtered[offset : offset + limit]

    # 分面计数：每个维度都在「除自己以外」的筛选条件下统计，且**与分页无关**——
    # 下拉里的数字永远是总数，翻页不会让它变。
    category_counts: Dict[str, int] = {}
    for tool in all_tools:
        if not _tool_matches(
            tool,
            want_category=None,
            want_plugin=plugin,
            keyword=keyword,
            diagnostic=ToolDiagnosticFilter.all,
        ):
            continue
        category_counts[tool["category"]] = category_counts.get(tool["category"], 0) + 1

    plugin_counts: Dict[str, int] = {}
    for tool in all_tools:
        if not _tool_matches(
            tool,
            want_category=category,
            want_plugin=None,
            keyword=keyword,
            diagnostic=ToolDiagnosticFilter.all,
        ):
            continue
        plugin_counts[tool["plugin"]] = plugin_counts.get(tool["plugin"], 0) + 1

    # 诊断概览：全量工具的健康度，不受任何筛选影响
    diagnostic_counts = {"total": len(all_tools), "empty_description": 0, "meta": 0, "ok": 0}
    for tool in all_tools:
        diag = _diagnose_tool(tool["name"], tool["description"], tool["category"])
        if diag.empty_description:
            diagnostic_counts["empty_description"] += 1
        elif diag.meta:
            diagnostic_counts["meta"] += 1
        else:
            diagnostic_counts["ok"] += 1

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "tools": page_items,
            "by_category": by_category,
            "by_plugin": by_plugin,
            "categories": sorted(list(categories_set)),
            "plugins": sorted(list(plugins_set)),
            "count": total,
            "total_count": len(all_tools),
            "category_counts": category_counts,
            "plugin_counts": plugin_counts,
            "diagnostic_counts": diagnostic_counts,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(page_items) < total,
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
        exclude_names=core_names,
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
            "core": [{"name": t.name, "plugin": _plugin_of(t.name)} for t in core_tools],
            "seeds": [{"name": t.name, "plugin": _plugin_of(t.name)} for t in seeds],
            "pool": [{"name": t.name, "plugin": _plugin_of(t.name)} for t in pool],
            "core_pool_size": len(core_names),
            "recall": recall,
            "max_extra": max_extra,
        },
    }


class EntityIndexEntry(BaseModel):
    """实体身份索引的单条记录。"""

    surface: str
    plugins: List[str]
    ambiguous: bool


class EntityIndexData(BaseModel):
    """实体身份索引的查询结果。

    `entries` 与 `items` 同内容：旧调用方读 `entries`，新前端读 `items`。
    """

    items: List[EntityIndexEntry]
    entries: List[EntityIndexEntry]
    count: int
    total: int
    total_all: int
    limit: Optional[int]
    offset: int
    has_more: bool


# 路径**不能**放在 /api/ai/tools/ 下：会被更早注册的 GET /api/ai/tools/{tool_name}
# 抢先匹配（把 entity_index 当成工具名）。
@app.get(
    "/api/ai/entity_index",
    summary="导出实体身份索引",
    tags=AI_TOOLS,
)
async def dump_entity_index(
    _auth: Any = Depends(require_auth_or_local_test),
    search: Optional[str] = Query(None, description="按 surface 或插件名模糊匹配"),
    ambiguous_only: bool = Query(False, description="只返回归属歧义（多插件冲突）的条目"),
    limit: Optional[int] = Query(None, ge=1, le=2000, description="单页条数；不传=全量"),
    offset: int = Query(0, ge=0, description="偏移量"),
) -> Dict[str, Any]:
    """导出 surface → 插件的实体身份索引。

    控制台诊断与评测 ground truth 共用。鉴权：webconsole Bearer **或** local-test。

    筛选与分页都在服务端做：`search` 匹配 surface 或插件名，`ambiguous_only` 只留
    多插件冲突的条目。`limit` **不传**时返回全量——评测脚本依赖旧契约拿整个索引。
    """
    from gsuid_core.ai_core.entity_index import get_entity_index

    keyword = (search or "").strip().lower()
    matched: List[EntityIndexEntry] = []
    index = get_entity_index()
    total_all = len(index)

    for surface, ref in index.items():
        ambiguous = ref.is_ambiguous
        if ambiguous_only and not ambiguous:
            continue
        if keyword:
            hit = keyword in surface.lower() or any(keyword in p.lower() for p in ref.plugins)
            if not hit:
                continue
        matched.append(EntityIndexEntry(surface=surface, plugins=list(ref.plugins), ambiguous=ambiguous))

    # 注册顺序随插件加载顺序变，分页必须按稳定序切，否则翻页会漏条目。
    matched.sort(key=lambda e: e.surface)

    total = len(matched)
    page = matched[offset:] if limit is None else matched[offset : offset + limit]

    return {
        "status": 0,
        "msg": "ok",
        "data": EntityIndexData(
            items=page,
            entries=page,
            count=total,
            total=total,
            total_all=total_all,
            limit=limit,
            offset=offset,
            has_more=offset + len(page) < total,
        ).model_dump(),
    }


@app.get("/api/ai/tools/capability_gaps", summary="能力缺口（find_tools 未命中）", tags=AI_TOOLS)
async def api_capability_gaps(
    limit: int = 20,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    gaps = get_capability_gaps(limit=max(1, min(limit, 100)))
    return {
        "status": 0,
        "msg": "ok",
        "data": [{"need": need, "count": count} for need, count in gaps],
    }
