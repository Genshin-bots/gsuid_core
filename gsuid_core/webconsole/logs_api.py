"""
Logs APIs
提供日志相关的 RESTful APIs
"""

import json
from typing import Dict, Optional
from datetime import datetime

from fastapi import Depends, Request
from fastapi.responses import StreamingResponse

from gsuid_core.logger import LOG_PATH, HistoryLogData, read_log
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


@app.get("/api/logs")
async def get_logs(
    request: Request,
    date: Optional[str] = None,
    level: Optional[str] = None,
    source: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    _user: Dict = Depends(require_auth),
):
    """Get logs with filtering and pagination"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    if date.endswith(".log"):
        date = date.removesuffix(".log")

    history_log_data = HistoryLogData()
    log_files = await history_log_data.get_parse_logs(LOG_PATH / f"{date}.log")

    # Filter by level
    if level and level != "all":
        log_files = [log for log in log_files if log.get("level") == level]

    # Filter by source
    if source and source != "all":
        log_files = [log for log in log_files if log.get("source") == source]

    total = len(log_files)
    start = (page - 1) * per_page
    end = start + per_page
    log_page = log_files[start:end]

    # Convert to frontend expected format
    formatted_logs = []
    level_mapping = {
        "info": "info",
        "warning": "warn",
        "warn": "warn",
        "error": "error",
        "debug": "debug",
        "critical": "error",
        "fatal": "error",
    }
    for i, log in enumerate(log_page):
        raw_level = log.get("日志等级", "INFO").lower()
        level = level_mapping.get(raw_level, "info")
        message = log.get("内容", "")
        if not isinstance(message, str):
            message = json.dumps(message, ensure_ascii=False)
        formatted_logs.append(
            {
                "id": start + i + 1,
                "timestamp": log.get("时间", ""),
                "level": level,
                "source": "core",
                "message": message,
                "details": None,
            }
        )

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "count": total,
            "rows": formatted_logs,
            "page": page,
            "per_page": per_page,
        },
    }


@app.get("/api/logs/sources")
async def get_log_sources(request: Request, _user: Dict = Depends(require_auth)):
    """Get available log sources"""
    return {
        "status": 0,
        "msg": "ok",
        "data": ["api", "auth", "database", "scheduler", "core"],
    }


@app.get("/api/logs/stream")
async def stream_logs(_user: Dict = Depends(require_auth)):
    """Stream real-time logs using Server-Sent Events"""
    return StreamingResponse(read_log(), media_type="text/event-stream")
