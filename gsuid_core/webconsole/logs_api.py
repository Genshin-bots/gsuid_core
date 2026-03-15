"""
Logs APIs
提供日志相关的 RESTful APIs
"""

import json
from typing import Dict, Optional
from datetime import datetime

from fastapi import Depends, Request
from fastapi.responses import StreamingResponse

from gsuid_core.logger import LOG_PATH, HistoryLogData, read_log, get_all_log_path
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
    log_file_path = LOG_PATH / f"{date}.log"
    if not log_file_path.exists():
        return {"status": 404, "msg": "该日志不存在", "data": None}
    log_files = await history_log_data.get_parse_logs(log_file_path)

    # Filter by level
    if level and level != "all":
        level_mapping = {
            "info": "info",
            "warning": "warn",
            "warn": "warn",
            "error": "error",
            "debug": "debug",
            "critical": "error",
            "fatal": "error",
        }
        filtered_logs = []
        for log in log_files:
            raw_level = log.get("日志等级", "INFO").lower()
            mapped_level = level_mapping.get(raw_level, "info")
            if mapped_level == level:
                filtered_logs.append(log)
        log_files = filtered_logs

    # Filter by source
    if source and source != "all":
        log_files = [log for log in log_files if log.get("来源", "core") == source]

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


@app.get("/api/logs/available-dates")
async def get_available_log_dates(
    _user: Dict = Depends(require_auth),
):
    """
    获取所有存在日志文件的日期列表，用于前端日历选择器标记可选择的日期

    Returns:
        包含以下字段的响应对象:
        - status: 状态码，0表示成功
        - msg: 状态信息
        - data: 日期字符串列表，按倒序排列(最新日期在前)，格式为YYYY-MM-DD
    """
    log_files = get_all_log_path()
    available_dates = [file.stem for file in log_files]
    available_dates.sort(reverse=True)
    return {"status": 0, "msg": "ok", "data": available_dates}


@app.get("/api/logs/sources")
async def get_log_sources(request: Request, _user: Dict = Depends(require_auth)):
    """Get available log sources"""
    return {
        "status": 0,
        "msg": "ok",
        "data": ["api", "auth", "database", "scheduler", "core"],
    }


@app.get("/api/logs/stats")
async def get_log_stats(
    request: Request,
    date: Optional[str] = None,
    level: Optional[str] = None,
    source: Optional[str] = None,
    per_page: int = 100,
    _user: Dict = Depends(require_auth),
):
    """Get log statistics (total count and page count) without fetching all logs"""
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    if date.endswith(".log"):
        date = date.removesuffix(".log")

    try:
        history_log_data = HistoryLogData()
        log_files = await history_log_data.get_parse_logs(LOG_PATH / f"{date}.log")

        level_mapping = {
            "info": "info",
            "warning": "warn",
            "warn": "warn",
            "error": "error",
            "debug": "debug",
            "critical": "error",
            "fatal": "error",
        }

        # Calculate statistics by level for entire date
        info_count = 0
        warn_count = 0
        error_count = 0
        debug_count = 0

        for log in log_files:
            raw_level = log.get("日志等级", "INFO").lower()
            mapped_level = level_mapping.get(raw_level, "info")
            if mapped_level == "info":
                info_count += 1
            elif mapped_level == "warn":
                warn_count += 1
            elif mapped_level == "error":
                error_count += 1
            elif mapped_level == "debug":
                debug_count += 1

        # Filter by level
        if level and level != "all":
            level_mapping = {
                "info": "info",
                "warning": "warn",
                "warn": "warn",
                "error": "error",
                "debug": "debug",
                "critical": "error",
                "fatal": "error",
            }
            filtered_logs = []
            for log in log_files:
                raw_level = log.get("日志等级", "INFO").lower()
                mapped_level = level_mapping.get(raw_level, "info")
                if mapped_level == level:
                    filtered_logs.append(log)
            log_files = filtered_logs

        # Filter by source
        if source and source != "all":
            log_files = [log for log in log_files if log.get("来源", "core") == source]

        total = len(log_files)
        total_pages = (total + per_page - 1) // per_page if per_page > 0 else 0

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "total": total,
                "total_pages": total_pages,
                "per_page": per_page,
                "info_count": info_count,
                "warn_count": warn_count,
                "error_count": error_count,
                "debug_count": debug_count,
            },
        }
    except Exception:
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "total": 0,
                "total_pages": 0,
                "per_page": per_page,
            },
        }


@app.get("/api/logs/stream")
async def stream_logs(_user: Dict = Depends(require_auth)):
    """Stream real-time logs using Server-Sent Events"""
    return StreamingResponse(read_log(), media_type="text/event-stream")
