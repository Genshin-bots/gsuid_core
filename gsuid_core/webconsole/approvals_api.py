"""统一审批中心 WebConsole API（三个裁决入口之一）。

- ``GET  /api/ai/approvals/list``                ：待审批 + 近期已裁决列表
- ``POST /api/ai/approvals/{request_id}/resolve`` ：裁决（approved / note）

Kanban 看板的 ``/api/ai/kanban/subtasks/{id}/approve`` 是本中心的领域兼容端点，
内部同样转 ``resolve_row``。
"""

from typing import Any, Dict, List

from fastapi import Query, Depends
from pydantic import BaseModel

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core import approval as approval_center
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

from ._api_tags import APPROVALS


class ResolveRequest(BaseModel):
    approved: bool
    note: str = ""


def _row_to_dict(r: "approval_center.AIApprovalRequest") -> Dict[str, Any]:
    return {
        "request_id": r.request_id,
        "short_id": r.short_id,
        "interaction": r.interaction,
        "audience": r.audience,
        "category": r.category,
        "ref_key": r.ref_key,
        "origin_session_id": r.origin_session_id,
        "operator_user_id": r.operator_user_id,
        "title": r.title,
        "status": r.status,
        "resolved_by": r.resolved_by,
        "resolved_note": r.resolved_note,
        "resolved_via": r.resolved_via,
        "created_at": r.created_at,
        "resolved_at": r.resolved_at,
    }


@app.get("/api/ai/approvals/list", summary="列出审批请求", tags=APPROVALS)
async def list_approvals(
    _: Dict[str, Any] = Depends(require_auth),
    status: str = Query("pending", description="pending=仅待审批；all=近期全部（含已裁决）"),
) -> Dict[str, Any]:
    """列出审批请求。"""
    await approval_center.expire_stale()
    if status == "all":
        rows = await approval_center.AIApprovalRequest.list_recent(limit=100)
    else:
        rows = await approval_center.AIApprovalRequest.list_pending()
    items: List[Dict[str, Any]] = [_row_to_dict(r) for r in rows]
    return {"status": 0, "msg": "ok", "data": {"items": items, "count": len(items)}}


@app.post("/api/ai/approvals/{request_id}/resolve", summary="处理审批请求", tags=APPROVALS)
async def resolve_approval(
    request_id: str,
    body: ResolveRequest,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """裁决一条审批请求（控制台登录后等同主人权限）。"""
    row = await approval_center.AIApprovalRequest.get_by_request_id(request_id)
    if row is None:
        row = await approval_center.AIApprovalRequest.get_by_short_id(request_id.lstrip("#"))
    if row is None:
        return {"status": 1, "msg": f"请求 {request_id} 不存在", "data": None}
    msg = await approval_center.resolve_row(
        row, body.approved, resolver_user_id=approval_center.CONSOLE_RESOLVER, note=body.note
    )
    return {"status": 0, "msg": msg, "data": _row_to_dict(row)}


logger.debug(t("log.webconsole.approvals_registered"))
