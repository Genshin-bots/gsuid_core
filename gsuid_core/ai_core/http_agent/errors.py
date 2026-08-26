"""Agent 面 JSON 错误体：``{code, message}``。404 保持 FastAPI 默认以免广告接口。"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from gsuid_core.ai_core.http_agent.types import ErrorBody


def error_response(status: int, code: str, message: str) -> JSONResponse:
    body: ErrorBody = {"code": code, "message": message}
    return JSONResponse(status_code=status, content=body)


def not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Not Found"})
