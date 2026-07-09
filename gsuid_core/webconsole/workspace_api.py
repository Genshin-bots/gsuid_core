"""v2 · Agent Mesh Kanban · Artifact Workspace WebAPI（``/api/ai/kanban/tasks/.../workspace/*``）。

设计稿见 docs/AGENT_MESH_COLLABORATION_PROPOSAL_20260521.md §12.4。

注意：``apply-patch`` 端点本期仅给出占位实现——把上传的 patch 文本登记为
``patch`` artifact 待人工审查后再应用，**不会**自动调用 git apply。这是有意为之：
"代理代理自动 git apply 仓库代码"是高风险动作，必须先经过人审。
"""

from typing import Any, Dict, List
from pathlib import Path
from datetime import datetime, timedelta

from fastapi import File, Query, Depends, UploadFile
from pydantic import BaseModel
from fastapi.responses import FileResponse

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.planning.models import AIAgentTask, AIAgentTaskLog, AIAgentArtifact
from gsuid_core.ai_core.planning.workspace import (
    DEFAULT_TTL_DAYS,
    task_workspace_root,
    register_workspace_artifacts,
)

from ._api_tags import WORKSPACE


def _safe_relpath(workspace: Path, requested: str) -> Path:
    """安全把请求路径解析回 workspace 子树内（防穿越）。"""
    import os

    full = (workspace / os.path.normpath(requested)).resolve()
    if not str(full).startswith(str(workspace.resolve())):
        raise PermissionError("越界路径")
    return full


@app.get("/api/ai/kanban/tasks/{task_id}/workspace/files", summary="列出工作区文件", tags=WORKSPACE)
async def list_workspace_files(
    task_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    task = await AIAgentTask.get_by_id(task_id)
    if task is None:
        return {"status": 1, "msg": "任务不存在", "data": None}
    ws = task_workspace_root(task.root_task_id or task.id, task.id)
    if not ws.exists():
        return {"status": 0, "msg": "ok", "data": {"workspace": str(ws), "files": []}}
    files: List[Dict[str, Any]] = []
    for p in ws.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = str(p.relative_to(ws))
            st = p.stat()
        except OSError:
            continue
        files.append(
            {
                "path": rel,
                "size_bytes": st.st_size,
                "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(),
            }
        )
    return {"status": 0, "msg": "ok", "data": {"workspace": str(ws), "files": files}}


@app.get("/api/ai/kanban/tasks/{task_id}/workspace/files/raw", summary="下载单文件", tags=WORKSPACE)
async def download_workspace_file(
    task_id: str,
    _: Dict[str, Any] = Depends(require_auth),
    path: str = Query(..., description="workspace 相对路径"),
):
    task = await AIAgentTask.get_by_id(task_id)
    if task is None:
        return {"status": 1, "msg": "任务不存在", "data": None}
    ws = task_workspace_root(task.root_task_id or task.id, task.id)
    try:
        full = _safe_relpath(ws, path)
    except PermissionError:
        return {"status": 1, "msg": "越界路径", "data": None}
    if not full.exists() or not full.is_file():
        return {"status": 1, "msg": "文件不存在", "data": None}
    return FileResponse(full, filename=full.name)


@app.post("/api/ai/kanban/tasks/{task_id}/workspace/import", summary="上传文件到 workspace", tags=WORKSPACE)
async def upload_workspace_file(
    task_id: str,
    _: Dict[str, Any] = Depends(require_auth),
    upload: UploadFile = File(...),
    sub_path: str = Query("", description="存放到 workspace 下的子目录（可空）"),
) -> Dict[str, Any]:
    task = await AIAgentTask.get_by_id(task_id)
    if task is None:
        return {"status": 1, "msg": "任务不存在", "data": None}
    root_id = task.root_task_id or task.id
    ws = task_workspace_root(root_id, task.id)
    ws.mkdir(parents=True, exist_ok=True)
    try:
        dest_dir = _safe_relpath(ws, sub_path) if sub_path else ws
    except PermissionError:
        return {"status": 1, "msg": "越界路径", "data": None}
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / (upload.filename or "uploaded.bin")
    contents = await upload.read()
    target.write_bytes(contents)
    arts = await register_workspace_artifacts(
        root_task_id=root_id,
        task_id=task_id,
        workspace=ws,
        changes=[(target, target.stat().st_size)],
        agent_profile=task.agent_profile,
        parent_task_id=task.parent_task_id,
    )
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "task_id": task_id,
            "path": str(target.relative_to(ws)),
            "size_bytes": target.stat().st_size,
            "artifact_ids": [a.id for a in arts],
        },
    }


class ApplyPatchRequest(BaseModel):
    """提交一段 patch 文本作为 ``patch`` artifact 待人审。

    本端点**不会**自动执行 ``git apply``——高风险动作必须人工确认。前端 UI 应
    引导主人看完 diff 后再决定是否在终端执行。
    """

    patch_text: str
    summary: str = "code patch"
    mime: str = "text/x-patch"


@app.post("/api/ai/kanban/tasks/{task_id}/workspace/apply-patch", summary="提交 patch（待人审）", tags=WORKSPACE)
async def submit_workspace_patch(
    task_id: str,
    body: ApplyPatchRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    task = await AIAgentTask.get_by_id(task_id)
    if task is None:
        return {"status": 1, "msg": "任务不存在", "data": None}
    root_id = task.root_task_id or task.id
    art = AIAgentArtifact(
        root_task_id=root_id,
        task_id=task_id,
        parent_task_id=task.parent_task_id,
        from_profile=task.agent_profile,
        artifact_kind="patch",
        mime=body.mime,
        summary=body.summary[:512],
        payload_inline=body.patch_text[:32000],
        size_bytes=len(body.patch_text.encode("utf-8")),
        expires_at=datetime.now() + timedelta(days=DEFAULT_TTL_DAYS),
    )
    await AIAgentArtifact.batch_insert_data([art])
    await AIAgentTaskLog.add_log(
        task_id,
        "decision",
        f"提交 patch artifact={art.id}（待人审，不自动应用）",
    )
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "artifact_id": art.id,
            "warning": "patch 已登记为 artifact，但框架不会自动 git apply；请人工审查后再应用。",
        },
    }
