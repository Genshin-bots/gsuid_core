"""
AI Knowledge Base APIs

提供 AI 知识库管理相关的 RESTful APIs，包括手动知识的增删改查、分页列表、搜索等。
手动添加的知识不会被启动时的插件同步流程检查、修改或删除。
"""

from typing import Any, Dict

from fastapi import Depends
from pydantic import BaseModel

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.rag.knowledge import (
    search_manual_knowledge,
    get_manual_knowledge_list,
    add_manual_knowledge_to_db,
    get_manual_knowledge_detail,
    update_manual_knowledge_in_db,
    delete_manual_knowledge_from_db,
)


class KnowledgeBaseCreate(BaseModel):
    """知识库新增请求模型"""

    plugin: str = "manual"
    title: str
    content: str
    tags: list[str]


class KnowledgeBaseUpdate(BaseModel):
    """知识库更新请求模型"""

    plugin: str = "manual"
    title: str = ""
    content: str = ""
    tags: list[str] = []


@app.get("/api/ai/knowledge/list")
async def get_knowledge_base_list(
    offset: int = 0,
    limit: int = 20,
    source: str = "all",
    page: int = 1,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取知识库列表（分页）

    Args:
        offset: 起始偏移，默认0（会被page参数覆盖）
        limit: 每页数量，默认20
        source: 来源过滤，默认"all"表示所有知识，"plugin"只查插件添加的，"manual"只查手动添加的
        page: 页码，从1开始，例如page=2表示第二页（offset=20）

    Returns:
        status: 0成功，1失败
        data: 包含知识列表、总数和分页信息
    """
    # 如果指定了page参数，计算offset
    if page > 1:
        offset = (page - 1) * limit

    result = await get_manual_knowledge_list(
        offset=offset,
        limit=limit,
        source_filter=source,
    )

    # 添加page信息到返回结果
    result["page"] = page
    result["page_size"] = limit

    return {
        "status": 0,
        "msg": "ok",
        "data": result,
    }


@app.get("/api/ai/knowledge/search")
async def search_knowledge_base(
    query: str,
    limit: int = 10,
    source: str = "all",
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    搜索知识库

    Args:
        query: 查询文本
        limit: 返回数量限制，默认10
        source: 来源过滤，默认"all"表示所有知识，"plugin"只搜插件添加的，"manual"只搜手动添加的

    Returns:
        status: 0成功，1失败
        data: 匹配的知识列表
    """
    results = await search_manual_knowledge(query=query, limit=limit, source_filter=source)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "results": results,
            "count": len(results),
            "query": query,
        },
    }


@app.get("/api/ai/knowledge/{entity_id}")
async def get_knowledge_base_detail(
    entity_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取指定知识库条目的详细信息

    Args:
        entity_id: 知识库 ID

    Returns:
        status: 0成功，1失败
        data: 知识详情
    """
    detail = await get_manual_knowledge_detail(entity_id)

    if detail is None:
        return {
            "status": 1,
            "msg": f"Knowledge '{entity_id}' not found",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": detail,
    }


@app.post("/api/ai/knowledge")
async def create_knowledge_base(
    knowledge: KnowledgeBaseCreate,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    新增手动知识库条目

    通过此接口添加的知识不会被启动时的插件同步流程检查、修改或删除。
    永久存在于向量数据库中，需要通过删除API进行删除。

    Args:
        knowledge: 知识库条目信息

    Returns:
        status: 0成功，1失败
        data: 新增结果
    """
    import uuid

    # 后端自动生成 ID
    knowledge_dict = knowledge.model_dump()
    knowledge_dict["id"] = str(uuid.uuid4())

    success = await add_manual_knowledge_to_db(knowledge_dict)

    if not success:
        return {
            "status": 1,
            "msg": "Failed to add knowledge to database",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "id": knowledge_dict["id"],
            "title": knowledge.title,
        },
    }


@app.put("/api/ai/knowledge/{entity_id}")
async def update_knowledge_base(
    entity_id: str,
    updates: KnowledgeBaseUpdate,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    更新手动添加的知识库条目

    通过此接口更新的知识不会被启动时的插件同步流程检查、修改或删除。

    Args:
        entity_id: 知识库 ID
        updates: 要更新的字段

    Returns:
        status: 0成功，1失败
        data: 更新结果
    """
    # 过滤掉空字符串和空列表字段，只更新有值的字段
    update_dict = updates.model_dump()
    update_dict = {k: v for k, v in update_dict.items() if v != "" and v != []}

    success = await update_manual_knowledge_in_db(entity_id, update_dict)

    if not success:
        return {
            "status": 1,
            "msg": f"Knowledge '{entity_id}' not found or update failed",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "id": entity_id,
        },
    }


@app.delete("/api/ai/knowledge/{entity_id}")
async def delete_knowledge_base(
    entity_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    删除手动添加的知识库条目

    通过此接口删除的知识是从向量数据库中永久删除，不会被恢复。

    Args:
        entity_id: 知识库 ID

    Returns:
        status: 0成功，1失败
        data: 删除结果
    """
    success = await delete_manual_knowledge_from_db(entity_id)

    if not success:
        return {
            "status": 1,
            "msg": f"Knowledge '{entity_id}' not found or delete failed",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "id": entity_id,
        },
    }
