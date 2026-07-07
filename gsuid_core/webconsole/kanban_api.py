"""v2 · Agent Mesh Kanban · WebAPI（前端通过 /api/ai/kanban/* 拉看板）。

设计稿见 docs/AGENT_MESH_COLLABORATION_PROPOSAL_20260521.md §12。

接口约定：
- 不做 WebSocket；前端通过刷新按钮 / setInterval polling 调 GET 端点；
- 写操作（pause / resume / fail / respawn / approve）走 ``planning.kanban`` /
  ``planning.kanban_executor``，与 LLM 工具共用底层 manager；
- 看板列由 ``kanban.compute_kanban_column`` 计算，前端拿到 5 列直接渲染。
"""

from typing import Any, Dict, List, Optional
from datetime import datetime

from fastapi import Query, Depends
from pydantic import BaseModel

from gsuid_core.logger import logger
from gsuid_core.ai_core.planning import kanban
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.planning.models import (
    AIAgentTask,
    AIAgentTaskLog,
    AIAgentArtifact,
)
from gsuid_core.ai_core.planning.workspace import task_workspace_root
from gsuid_core.ai_core.planning.kanban_executor import kick_root

# ─────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────


class TaskFailRequest(BaseModel):
    reason: str = "由网页控制台终止"
    cascade: bool = True


class SubtaskRespawnRequest(BaseModel):
    new_description: Optional[str] = None
    new_params: Optional[Dict[str, Any]] = None
    new_agent_profile: Optional[str] = None


class SubtaskApproveRequest(BaseModel):
    approved: bool
    note: str = ""


class SubtaskPatchRequest(BaseModel):
    display_name: Optional[str] = None
    goal: Optional[str] = None
    agent_profile: Optional[str] = None
    dependency_task_ids: Optional[List[str]] = None
    params_override: Optional[Dict[str, Any]] = None


class KanbanCreateRequest(BaseModel):
    goal: str
    persona_name: Optional[str] = None
    bot_id: str = ""
    owner_user_id: str = ""
    subtasks: List[Dict[str, Any]] = []


# ─────────────────────────────────────────────
# Serializers
# ─────────────────────────────────────────────


def _task_card(
    task: AIAgentTask,
    *,
    children: Optional[List[AIAgentTask]] = None,
    deps_satisfied: bool = True,
) -> Dict[str, Any]:
    workspace = (
        str(task_workspace_root(task.root_task_id or task.id, task.id))
        if task.node_kind == "subtask" or task.root_task_id
        else ""
    )
    subtask_count = len(children) if children is not None else 0
    subtask_done = sum(1 for c in children if c.status in ("completed", "skipped")) if children is not None else 0
    return {
        "kind": task.node_kind or "root",
        "id": task.id,
        "parent_task_id": task.parent_task_id,
        "root_task_id": task.root_task_id or task.id,
        "ordinal": task.ordinal,
        "display": task.display_name or task.goal[:48],
        "goal": task.goal,
        "status": task.status,
        "kanban_column": kanban.compute_kanban_column(task, deps_satisfied=deps_satisfied),
        "agent_profile": task.agent_profile,
        "persona_name": task.persona_name,
        "dependency_task_ids": task.dependency_task_ids or [],
        "respawn_count": task.respawn_count,
        "failure_reason": task.failure_reason,
        "input_artifact_ids": task.input_artifact_ids or [],
        "output_artifact_id": task.output_artifact_id,
        "workspace_path": workspace,
        "subtask_count": subtask_count,
        "subtask_done_count": subtask_done,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _log_dict(log: AIAgentTaskLog) -> Dict[str, Any]:
    return {
        "id": log.id,
        "task_id": log.task_id,
        "step_id": log.step_id,
        "timestamp": log.timestamp.isoformat() if log.timestamp else None,
        "event_type": log.event_type,
        "content": log.content,
    }


# ─────────────────────────────────────────────
# GET /api/ai/kanban/board
# ─────────────────────────────────────────────


@app.get("/api/ai/kanban/board")
async def get_kanban_board(
    _: Dict = Depends(require_auth),
    scope_key: Optional[str] = Query(None),
    bot_id: Optional[str] = Query(None),
    group_id: Optional[str] = Query(None),
    owner_user_id: Optional[str] = Query(None),
    include_children: bool = Query(True),
    status: Optional[str] = Query(None, description="按原始状态筛选"),
) -> Dict[str, Any]:
    """5 列 Kanban 看板（target / progress / Done / Blocked / failed）。"""
    roots = await kanban.list_root_tasks(
        scope_key=scope_key,
        bot_id=bot_id,
        group_id=group_id,
        owner_user_id=owner_user_id,
        status=status,
    )

    columns: Dict[str, List[Dict[str, Any]]] = {
        "target": [],
        "progress": [],
        "Done": [],
        "Blocked": [],
        "failed": [],
    }
    now = datetime.now()
    for root in roots:
        children: List[AIAgentTask] = []
        if include_children:
            _root_task, children = await kanban.get_task_tree(root.id)
        root_card = _task_card(root, children=children, deps_satisfied=True)
        columns.setdefault(root_card["kanban_column"], []).append(root_card)
        if include_children:
            done_ids = {c.id for c in children if c.status in ("completed", "skipped")}
            for c in children:
                deps = c.dependency_task_ids if isinstance(c.dependency_task_ids, list) else []
                deps_ok = all(d in done_ids for d in deps)
                card = _task_card(c, deps_satisfied=deps_ok)
                columns.setdefault(card["kanban_column"], []).append(card)

    subtask_total = sum(1 for col_cards in columns.values() for card in col_cards if card.get("kind") == "subtask")
    summary = {
        "task_count": len(roots),
        "subtask_count": subtask_total,
        "updated_at": now.isoformat(),
    }
    return {"status": 0, "msg": "ok", "data": {"columns": columns, "summary": summary}}


# ─────────────────────────────────────────────
# GET /api/ai/kanban/tasks/{task_id}
# ─────────────────────────────────────────────


@app.get("/api/ai/kanban/tasks/{task_id}")
async def get_kanban_task_detail(
    task_id: str,
    _: Dict = Depends(require_auth),
    log_limit: int = Query(200, ge=1, le=2000),
) -> Dict[str, Any]:
    task = await AIAgentTask.get_by_id(task_id)
    if task is None:
        return {"status": 1, "msg": f"任务 {task_id} 不存在", "data": None}
    root_id = task.root_task_id or task.id
    root, children = await kanban.get_task_tree(root_id)
    logs = await AIAgentTaskLog.get_for_task(task_id, limit=log_limit)
    artifacts = await AIAgentArtifact.list_for_task(task_id)
    # 同时把根任务（仅 root） / 子任务列表（root 视角）下的全部 artifact 也拉出来——
    # 让前端在"根任务详情"页能一次性展示整棵树所有产物，避免点开每个子任务再请求。
    root_artifacts: List[AIAgentArtifact] = []
    if root and root.id != task_id:
        root_artifacts = await AIAgentArtifact.list_for_root(root_id)
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "task": _task_card(task, children=children),
            "root": _task_card(root, children=children) if root else None,
            "subtasks": [_task_card(c) for c in children] if children else [],
            "logs": [_log_dict(lg) for lg in logs],
            "artifacts": [_artifact_card(a) for a in artifacts],
            "root_artifacts": [_artifact_card(a) for a in root_artifacts],
        },
    }


# 单一序列化函数：详情页 + 看板预览公用。前端只需读这份结构即可决定渲染策略。
def _artifact_card(a: AIAgentArtifact) -> Dict[str, Any]:
    """把 artifact 序列化为详情页 / 看板用的卡片结构。

    增强点：
    - inline 文本 / 小落盘文件直接吐 ``payload_preview``（≤ 8KB），省一次 HTTP；
    - 任何落盘 artifact 都带 ``raw_url`` = ``/api/ai/artifacts/{id}/raw``，前端
      ``<img>`` 直接挂这个 URL 渲染图片，文本 / PDF 也走同一通路；
    - ``is_image`` 帮前端判定要不要走 image 渲染分支。
    """
    payload_preview: Optional[str] = None
    if a.payload_inline:
        payload_preview = a.payload_inline[:8000]
    elif a.payload_path:
        # 仅对文本类 mime 自动读出 inline 预览；二进制不读避免乱码 / 大文件 OOM
        is_text_like = (a.mime or "").startswith("text/") or (a.mime or "") in (
            "application/json",
            "application/xml",
            "application/x-yaml",
        )
        if is_text_like and a.size_bytes and a.size_bytes <= 64 * 1024:
            from pathlib import Path

            try:
                payload_preview = Path(a.payload_path).read_text(encoding="utf-8", errors="replace")[:8000]
            except OSError:
                payload_preview = None
    raw_url: Optional[str] = None
    if a.payload_path:
        raw_url = f"/api/ai/artifacts/{a.id}/raw"
    return {
        "id": a.id,
        "kind": a.artifact_kind,
        "summary": a.summary,
        "mime": a.mime,
        "size_bytes": a.size_bytes,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "from_profile": a.from_profile,
        "task_id": a.task_id,
        "is_image": bool((a.mime or "").startswith("image/")),
        "has_inline": bool(a.payload_inline),
        "has_payload_path": bool(a.payload_path),
        "payload_preview": payload_preview,
        "raw_url": raw_url,
    }


# ─────────────────────────────────────────────
# POST /api/ai/kanban/tasks（管理端手动创建）
# ─────────────────────────────────────────────


@app.post("/api/ai/kanban/tasks")
async def admin_create_kanban_task(
    body: KanbanCreateRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """管理端绕过 LLM 评估直接创建任务树（用于演示 / 调试）。

    生产中创建 Kanban 任务应走 ``register_kanban_task`` LLM 工具（强校验评估覆盖）。
    """
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key

    if not body.goal.strip() or not body.subtasks:
        return {"status": 1, "msg": "goal / subtasks 不能为空", "data": None}

    scope_key = make_scope_key(ScopeType.USER_GLOBAL, body.owner_user_id or "admin")
    root, children = await kanban.create_kanban_tree(
        goal=body.goal,
        owner_user_id=body.owner_user_id or "admin",
        scope_key=scope_key,
        bot_id=body.bot_id,
        persona_name=body.persona_name,
        broadcast_targets=[],
        display_name=body.goal[:64],
        subtasks=body.subtasks,
    )
    import asyncio

    asyncio.create_task(kick_root(root.id))
    return {
        "status": 0,
        "msg": "ok",
        "data": {"task": _task_card(root, children=children), "subtasks": [_task_card(c) for c in children]},
    }


# ─────────────────────────────────────────────
# 状态操作端点
# ─────────────────────────────────────────────


@app.post("/api/ai/kanban/tasks/{task_id}/pause")
async def pause_kanban_task(task_id: str, _: Dict = Depends(require_auth)) -> Dict[str, Any]:
    task = await AIAgentTask.get_by_id(task_id)
    if task is None:
        return {"status": 1, "msg": "任务不存在", "data": None}
    ok = await kanban.pause_task(task_id)
    return {
        "status": 0 if ok else 1,
        "msg": "ok" if ok else "任务不在可暂停的状态",
        "data": {"task_id": task_id, "status": "paused" if ok else task.status},
    }


@app.post("/api/ai/kanban/tasks/{task_id}/resume")
async def resume_kanban_task(task_id: str, _: Dict = Depends(require_auth)) -> Dict[str, Any]:
    task = await AIAgentTask.get_by_id(task_id)
    if task is None:
        return {"status": 1, "msg": "任务不存在", "data": None}
    ok = await kanban.resume_task(task_id)
    if not ok:
        return {"status": 1, "msg": "任务不在可恢复的状态", "data": None}
    import asyncio

    asyncio.create_task(kick_root(task.root_task_id or task.id))
    return {"status": 0, "msg": "ok", "data": {"task_id": task_id, "status": "running"}}


@app.post("/api/ai/kanban/tasks/{task_id}/fail")
async def fail_kanban_task(
    task_id: str,
    body: TaskFailRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    task = await AIAgentTask.get_by_id(task_id)
    if task is None:
        return {"status": 1, "msg": "任务不存在", "data": None}
    if task.node_kind == "root" and body.cascade:
        ok = await kanban.fail_task_tree(task_id, body.reason)
        return {
            "status": 0 if ok else 1,
            "msg": "ok" if ok else "终结失败",
            "data": {"task_id": task_id, "status": "failed"},
        }
    # 子任务：只 fail 当前节点
    await kanban.mark_subtask_failed(task, body.reason)
    if task.root_task_id:
        await kanban.refresh_root_status(task.root_task_id)
    return {"status": 0, "msg": "ok", "data": {"task_id": task_id, "status": "failed"}}


@app.delete("/api/ai/kanban/tasks/{task_id}/hard")
async def hard_delete_kanban_task(
    task_id: str,
    delete_files: bool = Query(True, description="是否同时删除 data/ai_core/artifacts 下的 workspace / payload 文件"),
    include_instances: bool = Query(False, description="删除周期模板时是否同时删除它克隆出的历史实例树"),
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """硬删除 Kanban 任务树。

    这是不可逆的危险操作：会删除任务节点、任务日志、artifact 登记，以及可选的
    workspace / payload 文件。传入子任务 id 时也会删除其所属整棵根任务树，避免
    依赖边断裂。
    """
    ok, msg, stats = await kanban.hard_delete_task_tree(
        task_id,
        delete_files=delete_files,
        include_instances=include_instances,
    )
    return {"status": 0 if ok else 1, "msg": msg, "data": stats or None}


@app.delete("/api/ai/kanban/tasks")
async def bulk_delete_kanban_tasks(
    _: Dict = Depends(require_auth),
    scope_key: Optional[str] = Query(None, description="作用域筛选"),
    bot_id: Optional[str] = Query(None, description="关联 bot 筛选"),
    group_id: Optional[str] = Query(None, description="群号筛选"),
    owner_user_id: Optional[str] = Query(None, description="任务发起人筛选"),
    status: Optional[str] = Query(
        None,
        description="按原始状态筛选，如 completed / failed / running / pending / paused / waiting_approval / cancelled",
    ),
    delete_files: bool = Query(True, description="是否同时删除 data/ai_core/artifacts 下的 workspace / payload 文件"),
    include_instances: bool = Query(False, description="删除周期模板时是否同时删除它克隆出的历史实例树"),
) -> Dict[str, Any]:
    """批量删除 Kanban 任务树（按分类选择）。

    通过 Query 参数筛选根任务，然后逐棵硬删除整棵树。
    不传任何筛选条件时**不会**执行删除，返回 ``status=1`` 提示需要加条件。
    """
    has_filter = any(v is not None and v != "" for v in (scope_key, bot_id, group_id, owner_user_id, status))
    if not has_filter:
        return {
            "status": 1,
            "msg": (
                "批量删除必须至少指定一个筛选条件"
                "（scope_key / bot_id / group_id / owner_user_id / status），防止误删全部任务"
            ),
            "data": None,
        }

    deleted, failed, stats = await kanban.bulk_delete_task_trees(
        scope_key=scope_key,
        bot_id=bot_id,
        group_id=group_id,
        owner_user_id=owner_user_id,
        status=status,
        delete_files=delete_files,
        include_instances=include_instances,
    )
    return {
        "status": 0,
        "msg": f"批量删除完成：成功 {deleted} 棵，失败 {failed} 棵",
        "data": {
            "deleted_count": deleted,
            "failed_count": failed,
            "matched_count": deleted + failed,
            "tasks_deleted": stats["tasks_deleted"],
            "logs_deleted": stats["logs_deleted"],
            "artifacts_deleted": stats["artifacts_deleted"],
            "files_deleted": stats["files_deleted"],
            "dirs_deleted": stats["dirs_deleted"],
            "unscheduled_jobs": stats["unscheduled_jobs"],
            "root_ids": stats["root_ids"],
            "failed_root_ids": stats["failed_root_ids"],
        },
    }


@app.post("/api/ai/kanban/subtasks/{task_id}/respawn")
async def respawn_kanban_subtask(
    task_id: str,
    body: SubtaskRespawnRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    task = await AIAgentTask.get_by_id(task_id)
    if task is None or task.node_kind != "subtask":
        return {"status": 1, "msg": "子任务不存在", "data": None}
    ok, msg = await kanban.respawn_child_task(
        task,
        new_description=body.new_description,
        new_params=body.new_params,
        new_agent_profile=body.new_agent_profile,
    )
    if ok and task.root_task_id:
        import asyncio

        asyncio.create_task(kick_root(task.root_task_id))
    return {"status": 0 if ok else 1, "msg": msg, "data": {"task_id": task_id}}


@app.post("/api/ai/kanban/subtasks/{task_id}/approve")
async def approve_kanban_subtask(
    task_id: str,
    body: SubtaskApproveRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """兼容端点：内部统一转到审批中心（category=kanban_subtask）裁决。"""
    task = await AIAgentTask.get_by_id(task_id)
    is_leaf_root = task is not None and task.node_kind == "root" and bool(task.agent_profile)
    if task is None or (task.node_kind != "subtask" and not is_leaf_root):
        return {"status": 1, "msg": "子任务不存在", "data": None}

    from gsuid_core.ai_core import approval as approval_center

    rows = await approval_center.AIApprovalRequest.list_pending(category="kanban_subtask", ref_key=task_id)
    if not rows:
        # 升级前遗留的无票据 waiting_approval：补开一张票再走统一裁决，账本不留暗路
        if task.status != "waiting_approval":
            return {"status": 1, "msg": f"子任务状态 {task.status} 不在待审批", "data": None}
        row = await approval_center.submit(
            category="kanban_subtask",
            title=f"任务#{task.ordinal}｜{task.display_name}（webconsole 补票）",
            audience="master",
            ref_key=task_id,
            operator_user_id=task.owner_user_id,
            origin_session_id=task.session_id or "",
            payload={"task_id": task_id, "root_task_id": task.root_task_id or ""},
        )
        rows = [row]
    msg = await approval_center.resolve_row(
        rows[0], body.approved, resolver_user_id=approval_center.CONSOLE_RESOLVER, note=body.note
    )
    return {"status": 0, "msg": msg, "data": {"task_id": task_id}}


@app.patch("/api/ai/kanban/subtasks/{task_id}")
async def patch_kanban_subtask(
    task_id: str,
    body: SubtaskPatchRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    task = await AIAgentTask.get_by_id(task_id)
    if task is None or task.node_kind != "subtask":
        return {"status": 1, "msg": "子任务不存在", "data": None}
    update_data: Dict[str, Any] = {}
    if body.display_name is not None:
        update_data["display_name"] = body.display_name[:128]
    if body.goal is not None:
        update_data["goal"] = body.goal[:2000]
    if body.agent_profile is not None:
        update_data["agent_profile"] = body.agent_profile[:64]
    if body.dependency_task_ids is not None:
        update_data["dependency_task_ids"] = body.dependency_task_ids
    if body.params_override is not None:
        update_data["params_override"] = body.params_override
    if not update_data:
        return {"status": 1, "msg": "未传任何待更新字段", "data": None}
    await AIAgentTask.update_data_by_data(select_data={"id": task_id}, update_data=update_data)
    await AIAgentTaskLog.add_log(task_id, "decision", f"webconsole 修改字段：{list(update_data.keys())}")
    return {"status": 0, "msg": "ok", "data": {"task_id": task_id}}


# ─────────────────────────────────────────────
# 能力评估端点（前端按钮）
# ─────────────────────────────────────────────


class EvaluateMeshRequest(BaseModel):
    user_goal: str
    owner_user_id: str = ""
    persona_name: str = ""


@app.post("/api/ai/capability-agents/evaluate-mesh")
async def trigger_capability_evaluation(
    body: EvaluateMeshRequest,
    _: Dict = Depends(require_auth),
) -> Dict[str, Any]:
    """前端"测试评估覆盖"按钮触发——一次性跑 capability_evaluator 并返回结果。"""
    from gsuid_core.ai_core.capability_agents.evaluator import evaluate_capability

    if not body.user_goal.strip():
        return {"status": 1, "msg": "user_goal 不能为空", "data": None}
    result = await evaluate_capability(
        user_goal=body.user_goal,
        owner_user_id=body.owner_user_id or "webconsole",
        persona_name=body.persona_name,
    )
    return {"status": 0, "msg": "ok", "data": result.to_dict()}


@app.get("/api/ai/capability-agents/kanban-candidates")
async def list_kanban_candidates(_: Dict = Depends(require_auth)) -> Dict[str, Any]:
    """返回可用于 Kanban 任务树的代理列表（不含内部 capability_evaluator）。"""
    from gsuid_core.ai_core.agent_node import list_nodes

    out: List[Dict[str, Any]] = []
    for p in list_nodes():
        if p.node_id == "capability_evaluator":
            continue
        out.append(
            {
                "node_id": p.node_id,
                "display_name": p.display_name,
                "when_to_use": p.when_to_use,
                "match_keywords": p.match_keywords,
                "tool_names": p.tool_names,
                "source": p.source,
            }
        )
    return {"status": 0, "msg": "ok", "data": {"items": out, "count": len(out)}}


logger.debug("📋 [Kanban] WebAPI /api/ai/kanban/* registered.")
