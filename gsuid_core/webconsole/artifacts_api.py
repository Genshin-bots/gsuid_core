"""v2 · Agent Mesh Kanban · Artifact Hub WebAPI（``/api/ai/artifacts/*``）。

设计稿见 docs/AGENT_MESH_COLLABORATION_PROPOSAL_20260521.md §12.3。

权限边界：默认按 ``root_task_id`` / ``task_id`` 查询；不做跨任务树的合并查询。
"""

from typing import Any, Dict, Optional
from pathlib import Path
from datetime import datetime, timedelta

from fastapi import Query, Depends
from sqlmodel import col
from sqlalchemy import delete
from fastapi.responses import FileResponse

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.planning.models import AIAgentArtifact
from gsuid_core.utils.database.base_models import async_maker

from ._api_tags import ARTIFACTS


def _artifact_dict(a: AIAgentArtifact) -> Dict[str, Any]:
    return {
        "id": a.id,
        "root_task_id": a.root_task_id,
        "task_id": a.task_id,
        "parent_task_id": a.parent_task_id,
        "from_profile": a.from_profile,
        "artifact_kind": a.artifact_kind,
        "mime": a.mime,
        "summary": a.summary,
        "size_bytes": a.size_bytes,
        "has_inline": bool(a.payload_inline),
        "has_payload_path": bool(a.payload_path),
        "payload_path": a.payload_path,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
    }


@app.get("/api/ai/artifacts", summary="列表", tags=ARTIFACTS)
async def list_artifacts(
    _: Dict[str, Any] = Depends(require_auth),
    root_task_id: Optional[str] = Query(None),
    task_id: Optional[str] = Query(None),
) -> Dict[str, Any]:
    if task_id:
        items = await AIAgentArtifact.list_for_task(task_id)
    elif root_task_id:
        items = await AIAgentArtifact.list_for_root(root_task_id)
    else:
        return {"status": 1, "msg": "必须提供 root_task_id 或 task_id", "data": None}
    return {
        "status": 0,
        "msg": "ok",
        "data": {"items": [_artifact_dict(a) for a in items], "count": len(items)},
    }


@app.get("/api/ai/artifacts/{res_id}", summary="详情 + 预览", tags=ARTIFACTS)
async def get_artifact_detail(
    res_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    art = await AIAgentArtifact.get_by_id(res_id)
    if art is None:
        return {"status": 1, "msg": f"artifact {res_id} 不存在", "data": None}
    payload_preview: Optional[str] = art.payload_inline
    if not payload_preview and art.payload_path:
        try:
            payload_preview = Path(art.payload_path).read_text(encoding="utf-8", errors="replace")[:8000]
        except OSError:
            payload_preview = None
    detail = _artifact_dict(art)
    detail["payload_preview"] = payload_preview
    return {"status": 0, "msg": "ok", "data": detail}


@app.get("/api/ai/artifacts/{res_id}/raw", summary="下载原始 payload", tags=ARTIFACTS)
async def download_artifact_raw(
    res_id: str,
    _: Dict[str, Any] = Depends(require_auth),
):
    art = await AIAgentArtifact.get_by_id(res_id)
    if art is None or not art.payload_path:
        return {"status": 1, "msg": "无可下载的 payload_path", "data": None}
    p = Path(art.payload_path)
    if not p.exists():
        return {"status": 1, "msg": "落盘文件不存在", "data": None}
    return FileResponse(p, media_type=art.mime or "application/octet-stream")


@app.delete("/api/ai/artifacts/{res_id}", summary="删除", tags=ARTIFACTS)
async def delete_artifact(
    res_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    art = await AIAgentArtifact.get_by_id(res_id)
    if art is None:
        return {"status": 1, "msg": "不存在", "data": None}
    # 文件落盘的尝试删除
    if art.payload_path:
        try:
            p = Path(art.payload_path)
            if p.exists():
                p.unlink()
        except OSError:
            pass
    async with async_maker() as session:
        await session.execute(delete(AIAgentArtifact).where(col(AIAgentArtifact.id) == res_id))
        await session.commit()
    return {"status": 0, "msg": "ok", "data": {"res_id": res_id}}


@app.post("/api/ai/artifacts/{res_id}/extend-ttl", summary="延长 TTL", tags=ARTIFACTS)
async def extend_artifact_ttl(
    res_id: str,
    _: Dict[str, Any] = Depends(require_auth),
    days: int = Query(30, ge=1, le=365),
) -> Dict[str, Any]:
    art = await AIAgentArtifact.get_by_id(res_id)
    if art is None:
        return {"status": 1, "msg": "不存在", "data": None}
    new_expire = datetime.now() + timedelta(days=days)
    await AIAgentArtifact.update_data_by_data(select_data={"id": res_id}, update_data={"expires_at": new_expire})
    return {"status": 0, "msg": "ok", "data": {"res_id": res_id, "expires_at": new_expire.isoformat()}}
