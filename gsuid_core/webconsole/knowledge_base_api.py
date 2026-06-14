"""
AI Knowledge Base APIs

提供 AI 知识库管理相关的 RESTful APIs，包括手动知识的增删改查、分页列表、搜索等。
手动添加的知识不会被启动时的插件同步流程检查、修改或删除。
"""

import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import Depends
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from fastapi.responses import StreamingResponse

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.rag.chunking import DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_OVERLAP
from gsuid_core.ai_core.rag.knowledge import (
    add_knowledge_document,
    import_manual_knowledge,
    search_manual_knowledge,
    delete_knowledge_document,
    get_manual_knowledge_list,
    add_manual_knowledge_to_db,
    get_manual_knowledge_detail,
    iter_export_manual_knowledge,
    update_manual_knowledge_in_db,
    deep_reconcile_manual_knowledge,
    delete_manual_knowledge_from_db,
)

# WebConsole 端点捕获的"合法运行时/DB 故障"：转成 status=1 返回前端，**不**包括编程错误
# （KeyError/AttributeError/TypeError/NameError 等）——后者应冒泡到 FastAPI 500 处理器，
# 避免被宽 except 吞掉、线上难以定位（见 CODE_REVIEW §4）。
_RUNTIME_ERRORS = (SQLAlchemyError, OSError, ValueError)


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


class KnowledgeBulkImport(BaseModel):
    """批量导入（服务端分片）请求模型

    full_text 与 items 二选一：full_text 由服务端按 chunk_size/overlap 分片；
    items 为客户端已分好的分片列表（每项含 content）。
    """

    title: str
    doc_id: Optional[str] = None
    full_text: Optional[str] = None
    items: Optional[List[dict]] = None
    tags: List[str] = []
    plugin: str = "manual"
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    replace: bool = True


class KnowledgeImportRecords(BaseModel):
    """从导出件恢复手动知识的请求模型（records 或 jsonl 至少一个）"""

    records: Optional[List[dict]] = None
    jsonl: Optional[str] = None


@app.get("/api/ai/knowledge/list")
async def get_knowledge_base_list(
    offset: int = 0,
    limit: int = 20,
    source: str = "all",
    page: int = 1,
    doc_id: Optional[str] = None,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取知识库列表（分页）

    Args:
        offset: 起始偏移，默认0（会被page参数覆盖）
        limit: 每页数量，默认20
        source: 来源过滤，默认"all"表示所有知识，"plugin"只查插件添加的，"manual"只查手动添加的
        page: 页码，从1开始，例如page=2表示第二页（offset=20）
        doc_id: 可选，仅列出某篇文档的分片（仅对 source=manual 生效）

    Returns:
        status: 0成功，1失败
        data: 包含知识列表、总数和分页信息

    Note:
        source=manual 走 SQL 真值源原生分页（offset 为 O(1)）；plugin/all 仍走 Qdrant scroll。
    """
    # 如果指定了page参数，计算offset
    if page > 1:
        offset = (page - 1) * limit

    result = await get_manual_knowledge_list(
        offset=offset,
        limit=limit,
        source_filter=source,
        doc_id=doc_id,
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


# ─────────────────────────────────────────────
# 批量导入 / 文档管理 / 备份导出导入
# 设计见 plans/knowledge_base_bulk_import_assessment_20260614.md §5
# ─────────────────────────────────────────────


@app.post("/api/ai/knowledge/bulk")
async def bulk_import_knowledge(
    req: KnowledgeBulkImport,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """批量导入一篇文档（服务端分片 + 批量嵌入 + 幂等入库）

    用于把数十万字长文一次导入：服务端按 chunk_size/overlap 分片，每片单独成向量，
    避免整段长文被嵌入模型按上限静默截断。同一 doc_id 重导即覆盖（幂等）。

    请求体：full_text（整篇，服务端分片）与 items（已分片数组）二选一。

    Returns:
        data: {doc_id, total_chunks, written, skipped}
    """
    if not req.full_text and not req.items:
        return {"status": 1, "msg": "full_text 与 items 至少提供一个", "data": None}

    doc_id = (req.doc_id or "").strip() or uuid.uuid4().hex
    try:
        result = await add_knowledge_document(
            doc_id=doc_id,
            title=req.title,
            full_text=req.full_text,
            items=req.items,
            tags=req.tags,
            plugin=req.plugin,
            chunk_size=req.chunk_size,
            chunk_overlap=req.chunk_overlap,
            replace=req.replace,
        )
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"批量导入失败: {e}", "data": None}

    return {"status": 0, "msg": "ok", "data": result}


@app.delete("/api/ai/knowledge/doc/{doc_id}")
async def delete_knowledge_doc(
    doc_id: str,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """删除整篇文档的全部分片（SQL 真值源 + Qdrant 向量）。"""
    try:
        result = await delete_knowledge_document(doc_id)
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"删除文档失败: {e}", "data": None}
    return {"status": 0, "msg": "ok", "data": result}


@app.get("/api/ai/knowledge/backup/export")
async def export_knowledge_backup(
    _: Dict = Depends(require_auth),
):
    """流式导出全部手动知识为 JSONL（每行一条），供用户级备份/迁移。

    手动知识真值源为 SQL（AIKnowledgeChunk）；导出件可经 /backup/import 原样恢复。
    """
    return StreamingResponse(
        iter_export_manual_knowledge(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=manual_knowledge.jsonl"},
    )


@app.post("/api/ai/knowledge/backup/import")
async def import_knowledge_backup(
    req: KnowledgeImportRecords,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """从导出件恢复手动知识（SQL 真值源 + 重嵌入）。

    请求体 records（dict 列表）或 jsonl（原始 JSONL 字符串）至少提供一个。

    Returns:
        data: {total, written, skipped}
    """
    records: List[dict] = req.records or []
    if not records and req.jsonl:
        for line in req.jsonl.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    records.append(obj)
            except json.JSONDecodeError:
                continue

    if not records:
        return {"status": 1, "msg": "records 或 jsonl 至少提供一个有效记录", "data": None}

    try:
        result = await import_manual_knowledge(records)
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"导入失败: {e}", "data": None}

    return {"status": 0, "msg": "ok", "data": result}


@app.post("/api/ai/knowledge/reconcile")
async def reconcile_knowledge(
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """手动知识深度对账（运维入口）。

    逐条比对手动知识的 SQL 真值源与 Qdrant 向量，修复"数量相等但内容分叉"的盲区：
    - Qdrant 有 / SQL 无 → 回填 SQL；
    - SQL 有 / Qdrant 无 → 从 SQL 重嵌入；
    - 两侧 content_hash 不一致 → 以 SQL 为真值源重嵌覆盖。

    比启动期的数量对账昂贵（全量 scroll + 全表读），建议在换嵌入模型 / 迁移 / 疑似数据
    分叉时手动触发。

    Returns:
        data: {sql_total, qdrant_total, backfilled, reembedded_missing,
               reembedded_mismatch, reembedded_written, consistent}
    """
    try:
        result = await deep_reconcile_manual_knowledge()
    except _RUNTIME_ERRORS as e:
        return {"status": 1, "msg": f"深度对账失败: {e}", "data": None}
    return {"status": 0, "msg": "ok", "data": result}
