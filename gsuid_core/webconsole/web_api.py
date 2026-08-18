"""
Web API for the new frontend
Provides RESTful APIs for the React frontend
按路由功能拆分为多个模块文件

导入说明：
- 所有功能模块都引用 app_app 中的 app 对象来定义路由
- 本文件作为聚合文件，统一导入并注册所有路由
"""

from typing import Any, Dict, Optional

from fastapi import Query, Header, Depends, HTTPException
from pydantic import BaseModel

from gsuid_core.webconsole.session_store import SessionRecord, session_store

TEMP_DICT: Dict[str, Dict[str, Any]] = {}


def verify_token(authorization: str | None = None, token: str | None = None) -> Optional[SessionRecord]:
    """校验会话。Bearer 优先；``?token=`` 仅给 EventSource / <img> 用。"""
    raw = ""
    if authorization and authorization.startswith("Bearer "):
        raw = authorization[7:]
    elif token:
        raw = token
    if not raw:
        return None
    return session_store.verify(raw)


def require_auth(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> SessionRecord:
    """FastAPI dependency for authentication"""
    user_data = verify_token(authorization, token)
    if not user_data:
        raise HTTPException(status_code=401, detail="未授权，请先登录")
    return user_data


def session_role(user_data: SessionRecord | None) -> str:
    if not isinstance(user_data, dict):
        return ""
    if "user" not in user_data:
        return ""
    user = user_data["user"]
    if not isinstance(user, dict):
        return ""
    if "role" not in user:
        return ""
    role = user["role"]
    return role if isinstance(role, str) else ""


def require_admin(user_data: SessionRecord = Depends(require_auth)) -> SessionRecord:
    """须 ``role == admin``。重启 / 装插件 / 改核心配置 / 库 / 备份 / MCP / git。"""
    if session_role(user_data) != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user_data


def require_auth_header(authorization: str | None = Header(default=None)) -> SessionRecord:
    """只认 ``Authorization: Bearer``，拒绝 ``?token=``，避免密钥接口把令牌写进 URL/日志/Referer。"""
    user_data = verify_token(authorization, None)
    if not user_data:
        raise HTTPException(status_code=401, detail="未授权，请先登录")
    return user_data


def require_admin_header(authorization: str | None = Header(default=None)) -> SessionRecord:
    """管理员 + 仅 Bearer。用于回传明文密钥的 GET。"""
    user_data = require_auth_header(authorization)
    if session_role(user_data) != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user_data


LIVECHAT_WS_BOT_ID = "webconsole_livechat"


def livechat_ws_authorized(token: str | None) -> bool:
    """控制台 Live Chat WS：必须持有有效登录会话，不用核心 WS_TOKEN。"""
    if not token:
        return False
    return verify_token(token=token) is not None


# ===================
# Response Models
# ===================


class ApiResponse(BaseModel):
    status: int = 0
    msg: str = "ok"
    data: Any = None
