"""Agent 可视化调试台 API（C10 / plans/agent_design_review.md 建议五）

长任务编排与双路记忆极复杂，管理员此前是"瞎子"。本模块提供三个面板的后端 API：

- **Memory Graph View**     : 浏览某 scope 的知识图谱 Edge，一键软删除错误 Edge。
- **Orchestration Board**   : 看板式展示所有 AIAgentTask / 步骤 / 日志，支持改写步骤。
- **Persona Evolution Inspector**: 查看 / 人工修正 self_model 演化层（承诺 / 偏好等）。

纯工具层，无架构风险；所有写操作需登录鉴权。
"""

from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from fastapi import Query, Depends
from pydantic import BaseModel
from sqlmodel import col, select

from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.database.base_models import async_maker


def _ok(data: Any) -> Dict[str, Any]:
    return {"status": 0, "msg": "success", "data": data}


def _err(msg: str) -> Dict[str, Any]:
    return {"status": -1, "msg": msg, "data": None}


# ──────────────────────────── Memory Graph View ────────────────────────────


@app.get("/api/agent_debug/memory/edges")
async def list_memory_edges(
    scope_key: str = Query(..., description="作用域 key，如 group:789012"),
    include_invalid: bool = Query(False, description="是否含已软删除 Edge"),
    limit: int = Query(200, le=1000),
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """列出某 scope 的知识图谱 Edge（Memory Graph View）。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    async with async_maker() as session:
        stmt = select(AIMemEdge).where(AIMemEdge.scope_key == scope_key)
        if not include_invalid:
            stmt = stmt.where(col(AIMemEdge.invalid_at).is_(None))
        stmt = stmt.order_by(col(AIMemEdge.valid_at).desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())

    return _ok(
        [
            {
                "id": e.id,
                "fact": e.fact,
                "source_entity_id": e.source_entity_id,
                "target_entity_id": e.target_entity_id,
                "mention_count": e.mention_count,
                "decay_score": e.decay_score,
                "valid_at": e.valid_at.isoformat() if e.valid_at else None,
                "invalid_at": e.invalid_at.isoformat() if e.invalid_at else None,
                "last_accessed": e.last_accessed.isoformat() if e.last_accessed else None,
            }
            for e in rows
        ]
    )


@app.post("/api/agent_debug/memory/edge/{edge_id}/invalidate")
async def invalidate_memory_edge(
    edge_id: str,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """软删除一条错误 Edge（设 invalid_at），不物理删除。"""
    from gsuid_core.ai_core.memory.database.models import AIMemEdge

    async with async_maker() as session:
        edge = (await session.execute(select(AIMemEdge).where(AIMemEdge.id == edge_id))).scalar_one_or_none()
        if edge is None:
            return _err("Edge 不存在")
        edge.invalid_at = datetime.now(timezone.utc)
        await session.commit()
    logger.info(f"💻 [AgentDebug] 管理员软删除 Edge: {edge_id}")
    return _ok({"edge_id": edge_id})


@app.get("/api/agent_debug/memory/conflicts")
async def list_memory_conflicts(
    scope_key: str = Query(...),
    limit: int = Query(100, le=500),
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """列出某 scope 的记忆矛盾记录（C11 Contradiction）。"""
    from gsuid_core.ai_core.memory.database.models import AIMemConflict

    async with async_maker() as session:
        rows = list(
            (
                await session.execute(
                    select(AIMemConflict)
                    .where(AIMemConflict.scope_key == scope_key)
                    .order_by(col(AIMemConflict.created_at).desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return _ok(
        [
            {
                "id": c.id,
                "fact_signature": c.fact_signature,
                "summary": c.summary,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ]
    )


# ─────────────────────────── Orchestration Board ───────────────────────────
# 任务调试看板：只列任务节点与日志，详细的 Kanban 操作走 /api/ai/kanban/*。


@app.get("/api/agent_debug/tasks")
async def list_agent_tasks(
    status: Optional[str] = Query(None, description="按状态过滤"),
    limit: int = Query(100, le=500),
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """看板式列出所有任务节点（含根 + 子任务）。"""
    from gsuid_core.ai_core.planning.models import AIAgentTask

    async with async_maker() as session:
        stmt = select(AIAgentTask)
        if status:
            stmt = stmt.where(AIAgentTask.status == status)
        stmt = stmt.order_by(col(AIAgentTask.updated_at).desc()).limit(limit)
        rows = list((await session.execute(stmt)).scalars().all())

    return _ok(
        [
            {
                "id": t.id,
                "ordinal": t.ordinal,
                "node_kind": t.node_kind,
                "root_task_id": t.root_task_id,
                "display_name": t.display_name,
                "goal": t.goal,
                "status": t.status,
                "owner_user_id": t.owner_user_id,
                "agent_profile": t.agent_profile,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            }
            for t in rows
        ]
    )


@app.get("/api/agent_debug/tasks/{task_id}")
async def get_agent_task_detail(
    task_id: str,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """任务节点详情：主记录 + 同树子任务 + 执行日志。"""
    from gsuid_core.ai_core.planning import kanban
    from gsuid_core.ai_core.planning.models import AIAgentTask, AIAgentTaskLog

    task = await AIAgentTask.get_by_id(task_id)
    if task is None:
        return _err("任务不存在")
    root_id = task.root_task_id or task.id
    root, children = await kanban.get_task_tree(root_id)
    logs = await AIAgentTaskLog.get_for_task(task_id, limit=200)

    return _ok(
        {
            "task": {
                "id": task.id,
                "ordinal": task.ordinal,
                "node_kind": task.node_kind,
                "root_task_id": task.root_task_id,
                "parent_task_id": task.parent_task_id,
                "display_name": task.display_name,
                "goal": task.goal,
                "status": task.status,
                "agent_profile": task.agent_profile,
                "failure_reason": task.failure_reason,
                "review_notes": task.review_notes,
                "broadcast_targets": task.broadcast_targets,
            },
            "root": (
                {
                    "id": root.id,
                    "ordinal": root.ordinal,
                    "display_name": root.display_name,
                    "status": root.status,
                }
                if root
                else None
            ),
            "subtasks": [
                {
                    "id": c.id,
                    "ordinal": c.ordinal,
                    "display_name": c.display_name,
                    "goal": c.goal,
                    "status": c.status,
                    "agent_profile": c.agent_profile,
                    "dependency_task_ids": c.dependency_task_ids,
                    "respawn_count": c.respawn_count,
                    "failure_reason": c.failure_reason,
                }
                for c in children
            ],
            "logs": [
                {
                    "event_type": lg.event_type,
                    "content": lg.content,
                    "timestamp": lg.timestamp.isoformat() if lg.timestamp else None,
                }
                for lg in logs
            ],
        }
    )


@app.post("/api/agent_debug/tasks/{task_id}/abort")
async def abort_agent_task(
    task_id: str,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """管理员手动终止一个任务节点（不级联子任务；整树终结请走 /api/ai/kanban/tasks/{id}/fail）。"""
    from gsuid_core.ai_core.planning import kanban

    await kanban.abort_task(task_id, "管理员从调试台终止")
    return _ok({"task_id": task_id})


# ─────────────────────── Persona Evolution Inspector ───────────────────────


@app.get("/api/agent_debug/self_model")
async def get_self_model_api(
    bot_id: str = Query("default"),
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """查看 self_model 演化层（承诺 / 偏好 / 反思）。"""
    from gsuid_core.ai_core.self_cognition import get_self_model

    model = await get_self_model(bot_id)
    return _ok(model)


class SelfModelEditRequest(BaseModel):
    bot_id: str = "default"
    field: str
    items: List[str]


@app.post("/api/agent_debug/self_model")
async def set_self_model_api(
    body: SelfModelEditRequest,
    _user: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """人工修正跑偏的 self_model 字段（整字段覆盖）。"""
    from gsuid_core.ai_core.self_cognition import _FIELDS, overwrite_self_model_field

    if body.field not in _FIELDS:
        return _err(f"非法字段，须为 {_FIELDS} 之一")
    await overwrite_self_model_field(body.bot_id, body.field, body.items)
    return _ok({"field": body.field, "count": len(body.items)})
