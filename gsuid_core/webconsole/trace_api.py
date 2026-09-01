"""
Trace APIs
提供追踪日志相关的 RESTful APIs
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, TypedDict

from fastapi import Depends

from gsuid_core.logger import trace_collector
from gsuid_core.trace_archive import (
    CommandTraceDayCount,
    CommandTraceListItem,
    daily_trace_counts,
    get_trace_from_jsonl,
    list_traces_from_jsonl,
    get_trace_logs_from_daily_log,
)
from gsuid_core.utils.path_safety import PathEscapeError, parse_iso_date
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

from ._api_tags import TRACE


class CommandTraceListPage(TypedDict):
    rows: list[CommandTraceListItem]
    count: int
    page: int
    per_page: int


def _clamp_page(page: int) -> int:
    return 1 if page < 1 else page


def _clamp_per_page(per_page: int) -> int:
    if per_page < 1:
        return 1
    if per_page > 100:
        return 100
    return per_page


def _empty_list_page(page: int, per_page: int) -> CommandTraceListPage:
    return {"rows": [], "count": 0, "page": page, "per_page": per_page}


def _running_list_item(trace_id: str, meta: Dict[str, object]) -> CommandTraceListItem | None:
    if "status" not in meta or meta["status"] != "running":
        return None
    if "command" not in meta or "user_id" not in meta or "start_time" not in meta or "log_count" not in meta:
        return None
    command = meta["command"]
    user_id = meta["user_id"]
    start_time = meta["start_time"]
    log_count = meta["log_count"]
    if not isinstance(command, str) or not isinstance(user_id, str):
        return None
    if isinstance(log_count, bool) or not isinstance(log_count, int):
        return None
    if isinstance(start_time, bool) or not isinstance(start_time, (int, float)):
        return None
    trace_meta = trace_collector.get_trace_meta(trace_id)
    return {
        "trace_id": trace_id,
        "command": command,
        "user_id": user_id,
        "group_id": trace_meta.group_id if trace_meta else None,
        "start_time": float(start_time),
        "duration_ms": None,
        "log_count": log_count,
        "status": "running",
    }


def merge_command_trace_list(date: str, page: int, per_page: int) -> CommandTraceListPage:
    merged: dict[str, CommandTraceListItem] = {}
    for record in list_traces_from_jsonl(date):
        merged[record["trace_id"]] = record
    for trace_id, meta in trace_collector.get_active_traces().items():
        row = _running_list_item(trace_id, meta)
        if row is None:
            continue
        merged[trace_id] = row
    result = list(merged.values())
    result.sort(key=lambda x: x["start_time"], reverse=True)
    total = len(result)
    per_page = _clamp_per_page(per_page)
    page = _clamp_page(page)
    max_page = max(1, (total + per_page - 1) // per_page) if total else 1
    if page > max_page:
        page = max_page
    start = (page - 1) * per_page
    return {
        "rows": result[start : start + per_page],
        "count": total,
        "page": page,
        "per_page": per_page,
    }


@app.get("/api/traces", summary="获取追踪列表（统一入口）", tags=TRACE)
async def get_traces(
    date: Optional[str] = None,
    page: int = 1,
    per_page: int = 100,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """获取追踪列表（统一入口）

    合并内存中的活跃追踪和 JSONL 中的已完成追踪，返回统一目录。
    合并规则：
    - JSONL 提供 completed 的完整数据（含 duration_ms）
    - 内存中的 running 覆盖 JSONL 中的同名记录（running 是最新实时状态）
    - 内存中的 completed 不覆盖 JSONL（JSONL 数据更完整）
    """
    page = _clamp_page(page)
    per_page = _clamp_per_page(per_page)
    try:
        day = parse_iso_date(date, default_today=True)
    except PathEscapeError:
        return {
            "status": 1,
            "msg": "非法日期",
            "data": _empty_list_page(page, per_page),
        }

    data = await asyncio.to_thread(merge_command_trace_list, day, page, per_page)
    return {"status": 0, "msg": "ok", "data": data}


# 注意：本路由必须声明在 `/api/traces/{trace_id}` **之前**，否则 FastAPI 会把
# "daily_counts" 当作 trace_id 匹配到详情路由（返回 404）。固定路径优先于路径参数。
@app.get("/api/traces/daily_counts", summary="获取每日命令数（日历选择器）", tags=TRACE)
async def get_trace_daily_counts(
    days: int = 60,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """近 N 天每天的命令数——供前端日历选择器判断哪些日期可点击。

    Query 参数：
    - ``days``: 回溯天数，默认 60（约两个月），自动夹取到 [1, 366]。

    ``data`` 为按日期升序的列表，每项 ``{date, count}``；``count == 0`` 表示当天
    无命令记录、日历上不可点击。今天的计数实时可见（running 追踪已计入）。
    """
    days = max(1, min(days, 366))
    data: list[CommandTraceDayCount] = await asyncio.to_thread(daily_trace_counts, days)
    return {"status": 0, "msg": "ok", "data": data}


def _str_field(record: dict[str, object], key: str) -> str:
    if key not in record:
        return ""
    value = record[key]
    return value if isinstance(value, str) else ""


def _optional_str_field(record: dict[str, object], key: str) -> str | None:
    if key not in record:
        return None
    value = record[key]
    if value is None:
        return None
    return value if isinstance(value, str) else None


def _optional_int_field(record: dict[str, object], key: str) -> int | None:
    if key not in record:
        return None
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _detail_from_disk(trace_id: str, day: str) -> dict[str, object] | None:
    meta = get_trace_from_jsonl(trace_id, day)
    if meta is None:
        return None
    logs = get_trace_logs_from_daily_log(trace_id, day)
    start_time = meta["start_time"] if "start_time" in meta else 0
    if isinstance(start_time, bool) or not isinstance(start_time, (int, float)):
        start_time = 0
    status = _str_field(meta, "status")
    return {
        "trace_id": trace_id,
        "command": _str_field(meta, "command"),
        "user_id": _str_field(meta, "user_id"),
        "group_id": _optional_str_field(meta, "group_id"),
        "bot_id": _str_field(meta, "bot_id"),
        "session_id": _str_field(meta, "session_id"),
        "start_time": float(start_time),
        "duration_ms": _optional_int_field(meta, "duration_ms"),
        "log_count": _optional_int_field(meta, "log_count"),
        "status": status if status else "completed",
        "logs": logs,
    }


@app.get("/api/traces/{trace_id}", summary="获取追踪详情", tags=TRACE)
async def get_trace_detail(
    trace_id: str,
    date: Optional[str] = None,
    _user: Dict[str, Any] = Depends(require_auth),
):
    """获取指定追踪的完整日志

    优先查内存；未命中时通过 trace_id 扫描 daily log 文件提取完整日志。
    """
    try:
        day = parse_iso_date(date, default_today=True)
    except PathEscapeError:
        return {"status": 1, "msg": "非法日期", "data": None}

    # 先查内存（内存只保留正在执行中的追踪，命中即说明该追踪仍在 running）
    memory_logs = trace_collector.get_trace_logs(trace_id)
    if memory_logs is not None:
        meta = trace_collector.get_trace_meta(trace_id)
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "trace_id": trace_id,
                "command": meta.command if meta else "",
                "user_id": meta.user_id if meta else "",
                "group_id": meta.group_id if meta else None,
                "bot_id": meta.bot_id if meta else "",
                "session_id": meta.session_id if meta else "",
                "start_time": meta.start_ts if meta else 0,
                "status": "running",
                "logs": [{"timestamp": e.timestamp, "level": e.level, "event": e.event} for e in memory_logs],
            },
        }

    detail = await asyncio.to_thread(_detail_from_disk, trace_id, day)
    if detail is None:
        return {"status": 404, "msg": "追踪不存在", "data": None}
    return {"status": 0, "msg": "ok", "data": detail}
