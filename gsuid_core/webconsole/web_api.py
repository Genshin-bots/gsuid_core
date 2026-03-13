"""
Web API for the new frontend
Provides RESTful APIs for the React frontend
按路由功能拆分为多个模块文件

导入说明：
- 所有功能模块都引用 app_app 中的 app 对象来定义路由
- 本文件作为聚合文件，统一导入并注册所有路由
"""

import hashlib
import secrets
from typing import Any, Dict, Optional
from datetime import datetime

from fastapi import Header
from pydantic import BaseModel

from gsuid_core.config import core_config

# In-memory token storage (in production, use database)
active_tokens: Dict[str, Dict[str, Any]] = {}
TEMP_DICT: Dict[str, Dict[str, Any]] = {}


def generate_token(username: str) -> str:
    """Generate a simple token for a user"""
    TOKEN_SECRET = core_config.get_config("REGISTER_CODE")
    random_part = secrets.token_hex(16)
    token_input = f"{username}:{random_part}:{TOKEN_SECRET}"
    token_hash = hashlib.sha256(token_input.encode()).hexdigest()
    return f"{username}:{token_hash}"


def verify_token(authorization: str | None = None, token: str | None = None) -> Optional[Dict[str, Any]]:
    """Verify token from Authorization header or query parameter"""
    if not authorization and not token:
        return None

    # Use token from query parameter if provided
    if token:
        auth_token = token
    elif authorization:
        # Check Bearer token format
        if not authorization.startswith("Bearer "):
            return None

        auth_token = authorization[7:]  # Remove "Bearer " prefix

    if auth_token in active_tokens:
        token_data = active_tokens[auth_token]
        # Check if token is still valid (24h expiry)
        if datetime.now() < token_data["expires"]:
            return token_data

    return None


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
