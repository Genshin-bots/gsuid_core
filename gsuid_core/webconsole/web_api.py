"""
Web API for the new frontend
Provides RESTful APIs for the React frontend
按路由功能拆分为多个模块文件

导入说明：
- 所有功能模块都引用 app_app 中的 app 对象来定义路由
- 本文件作为聚合文件，统一导入并注册所有路由
"""

from typing import Any, Dict, Optional

from fastapi import Header
from pydantic import BaseModel

from gsuid_core.webconsole.session_store import SessionRecord, session_store

TEMP_DICT: Dict[str, Dict[str, Any]] = {}


def verify_token(authorization: str | None = None, token: str | None = None) -> Optional[SessionRecord]:
    """Verify token from Authorization header or query parameter

    会话由 session_store 管理：持久化（重启不掉线）+ 48h 有效期 + 并发数限制。
    """
    # Use token from query parameter if provided
    if token:
        auth_token = token
    elif authorization and authorization.startswith("Bearer "):
        auth_token = authorization[7:]  # Remove "Bearer " prefix
    else:
        return None

    return session_store.verify(auth_token)


def require_auth(authorization: str | None = Header(default=None), token: str | None = None):
    """FastAPI dependency for authentication"""
    user_data = verify_token(authorization, token)
    if not user_data:
        from fastapi import HTTPException

        raise HTTPException(status_code=401, detail="未授权，请先登录")
    return user_data


# ===================
# Response Models
# ===================


class ApiResponse(BaseModel):
    status: int = 0
    msg: str = "ok"
    data: Any = None
