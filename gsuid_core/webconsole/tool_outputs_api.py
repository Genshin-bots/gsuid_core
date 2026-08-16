"""FileOS 工具落盘 WebAPI（``/api/ai/tool-outputs/*``）。

数据：``AIToolOutputRecord``（SQL 真身）+ 可选磁盘 payload + Qdrant ``tool_outputs`` 索引。
与 Kanban Artifact Hub（``/api/ai/artifacts`` / ``res_*``）是两套账本。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from pathlib import Path

from fastapi import Body, Query, Depends
from pydantic import Field, BaseModel
from fastapi.responses import FileResponse, PlainTextResponse

from gsuid_core.i18n import t
from gsuid_core.utils.path_safety import PathEscapeError, ensure_under_any
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.planning.workspace import ARTIFACT_ROOT
from gsuid_core.ai_core.planning.tool_output_index import delete_tool_output_index
from gsuid_core.ai_core.planning.tool_output_store import AIToolOutputRecord
from gsuid_core.ai_core.planning.tool_output_protocol import load_payload_text

from ._api_tags import TOOL_OUTPUTS


def _record_dict(rec: AIToolOutputRecord) -> Dict[str, Any]:
    return {
        "id": rec.id,
        "tool_name": rec.tool_name,
        "profile": rec.profile,
        "summary": rec.summary,
        "owner_user_id": rec.owner_user_id,
        "scope_key": rec.scope_key,
        "session_id": rec.session_id,
        "task_id": rec.task_id,
        "root_task_id": rec.root_task_id,
        "date_str": rec.date_str,
        "res_handle": rec.res_handle,
        "size_bytes": rec.size_bytes,
        "has_inline": bool(rec.payload_inline),
        "has_payload_path": bool(rec.payload_path),
        "payload_path": rec.payload_path or "",
        "content_hash": rec.content_hash or "",
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
    }


class BatchDeleteBody(BaseModel):
    ids: List[str] = Field(default_factory=list, description="FileOS 记录 id 列表（to_/sa_…）")


@app.get("/api/ai/tool-outputs", summary="列表（筛选+分页）", tags=TOOL_OUTPUTS)
async def list_tool_outputs(
    _: Dict[str, Any] = Depends(require_auth),
    tool_name: Optional[str] = Query(None),
    owner_user_id: Optional[str] = Query(None),
    scope_key: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None, description="匹配 summary / id / tool_name / session_id"),
    include_expired: bool = Query(False),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    rows, total = await AIToolOutputRecord.list_admin(
        tool_name=tool_name,
        owner_user_id=owner_user_id,
        scope_key=scope_key,
        session_id=session_id,
        keyword=keyword,
        include_expired=include_expired,
        limit=limit,
        offset=offset,
    )
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "items": [_record_dict(r) for r in rows],
            "count": len(rows),
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(rows) < total,
        },
    }


@app.get("/api/ai/tool-outputs/meta/tool-names", summary="工具名筛选项", tags=TOOL_OUTPUTS)
async def list_tool_output_tool_names(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    names = await AIToolOutputRecord.distinct_tool_names()
    return {"status": 0, "msg": "ok", "data": {"tool_names": names}}


@app.get("/api/ai/tool-outputs/{record_id}", summary="详情 + 预览", tags=TOOL_OUTPUTS)
async def get_tool_output_detail(
    record_id: str,
    _: Dict[str, Any] = Depends(require_auth),
    preview_chars: int = Query(12000, ge=500, le=100000),
) -> Dict[str, Any]:
    rec = await AIToolOutputRecord.get_by_id(record_id)
    if rec is None:
        return {
            "status": 1,
            "msg": t("msg.webconsole.tool_output.not_found", record_id=record_id),
            "data": None,
        }
    detail = _record_dict(rec)
    payload_path = rec.payload_path or ""
    if payload_path:
        try:
            payload_path = str(ensure_under_any(Path(payload_path), (ARTIFACT_ROOT,)))
        except PathEscapeError:
            payload_path = ""
    body, err = load_payload_text(
        payload_inline=rec.payload_inline,
        payload_path=payload_path,
    )
    if err:
        body = ""
    preview = body[:preview_chars] if body else None
    detail["payload_preview"] = preview
    detail["payload_truncated"] = bool(body and len(body) > preview_chars)
    detail["payload_full_chars"] = len(body) if body else 0
    return {"status": 0, "msg": "ok", "data": detail}


@app.get("/api/ai/tool-outputs/{record_id}/raw", summary="下载/查看全文", tags=TOOL_OUTPUTS)
async def download_tool_output_raw(
    record_id: str,
    _: Dict[str, Any] = Depends(require_auth),
):
    rec = await AIToolOutputRecord.get_by_id(record_id)
    if rec is None:
        return {
            "status": 1,
            "msg": t("msg.webconsole.tool_output.not_found", record_id=record_id),
            "data": None,
        }
    if rec.payload_path:
        try:
            p = ensure_under_any(Path(rec.payload_path), (ARTIFACT_ROOT,))
        except PathEscapeError:
            return {
                "status": 1,
                "msg": t("msg.webconsole.tool_output.file_not_found"),
                "data": None,
            }
        if p.exists() and p.is_file():
            return FileResponse(
                p,
                media_type="text/markdown; charset=utf-8",
                filename=f"{record_id}.md",
            )
        return {
            "status": 1,
            "msg": t("msg.webconsole.tool_output.file_not_found"),
            "data": None,
        }
    if rec.payload_inline is not None:
        return PlainTextResponse(
            rec.payload_inline,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{record_id}.md"'},
        )
    return {
        "status": 1,
        "msg": t("msg.webconsole.tool_output.no_payload"),
        "data": None,
    }


@app.delete("/api/ai/tool-outputs/{record_id}", summary="单条删除", tags=TOOL_OUTPUTS)
async def delete_tool_output(
    record_id: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    n, rids = await AIToolOutputRecord.delete_by_ids([record_id])
    if n == 0:
        return {
            "status": 1,
            "msg": t("msg.webconsole.tool_output.not_found", record_id=record_id),
            "data": None,
        }
    await delete_tool_output_index(rids)
    return {"status": 0, "msg": "ok", "data": {"id": record_id, "deleted": n}}


@app.post("/api/ai/tool-outputs/batch-delete", summary="批量删除", tags=TOOL_OUTPUTS)
async def batch_delete_tool_outputs(
    body: BatchDeleteBody = Body(...),
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    ids = [i.strip() for i in body.ids if isinstance(i, str) and i.strip()]
    if not ids:
        return {
            "status": 1,
            "msg": t("msg.webconsole.tool_output.ids_required"),
            "data": None,
        }
    # 上限防误操作
    if len(ids) > 500:
        return {
            "status": 1,
            "msg": t("msg.webconsole.tool_output.batch_limit"),
            "data": None,
        }
    n, rids = await AIToolOutputRecord.delete_by_ids(ids)
    if rids:
        await delete_tool_output_index(rids)
    return {
        "status": 0,
        "msg": "ok",
        "data": {"deleted": n, "ids": rids},
    }
