"""HTTP Agent 钥管理。Admin 建钥不依赖 AI 总开关与 ``enable_http_agent_api``。"""

from __future__ import annotations

from typing import Dict

from fastapi import Depends, APIRouter
from pydantic import Field, BaseModel

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_admin_header
from gsuid_core.ai_core.http_agent.keys import KeyStoreError, public_view, get_key_store

from ._api_tags import CORE_CONFIG

admin_router = APIRouter()


class CreateHttpAgentKeyRequest(BaseModel):
    user_id: str
    bot_id: str
    user_pm: int = Field(default=6, ge=0, le=6)
    persona: str = ""
    label: str = ""


@admin_router.post("/api/http-agent/admin/keys", summary="创建 HTTP Agent API Key", tags=CORE_CONFIG)
async def create_http_agent_key(
    body: CreateHttpAgentKeyRequest,
    _user: Dict[str, object] = Depends(require_admin_header),
) -> Dict[str, object]:
    store = get_key_store()
    try:
        token, rec = store.create(
            user_id=body.user_id,
            bot_id=body.bot_id,
            user_pm=body.user_pm,
            persona=body.persona,
            label=body.label,
        )
    except KeyStoreError as e:
        return {"status": 1, "msg": e.message, "data": None}
    data: Dict[str, object] = dict(public_view(rec))
    data["token"] = token
    return {"status": 0, "msg": "ok", "data": data}


@admin_router.get("/api/http-agent/admin/keys", summary="列出 HTTP Agent API Key", tags=CORE_CONFIG)
async def list_http_agent_keys(_user: Dict[str, object] = Depends(require_admin_header)) -> Dict[str, object]:
    keys = get_key_store().list_public()
    return {"status": 0, "msg": "ok", "data": keys}


@admin_router.post("/api/http-agent/admin/keys/{key_id}/revoke", summary="吊销 HTTP Agent API Key", tags=CORE_CONFIG)
async def revoke_http_agent_key(
    key_id: str,
    _user: Dict[str, object] = Depends(require_admin_header),
) -> Dict[str, object]:
    ok = get_key_store().revoke(key_id)
    if not ok:
        return {"status": 1, "msg": "key not found", "data": None}
    return {"status": 0, "msg": "ok", "data": {"key_id": key_id, "revoked": True}}


app.include_router(admin_router)
