"""
Trace APIs
提供追踪日志相关的 RESTful APIs
"""

from typing import Dict, Optional
from datetime import datetime

from fastapi import Depends

from gsuid_core.logger import trace_collector
from gsuid_core.trace_archive import (
    daily_trace_counts,
    get_trace_from_jsonl,
    list_traces_from_jsonl,
    get_trace_logs_from_daily_log,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


@app.get("/api/traces")
async def get_traces(
    date: Optional[str] = None,
    limit: int = 500,
    _user: Dict = Depends(require_auth),
):
    """获取追踪列表（统一入口）

    合并内存中的活跃追踪和 JSONL 中的已完成追踪，返回统一目录。
    合并规则：
    - JSONL 提供 completed 的完整数据（含 duration_ms）
    - 内存中的 running 覆盖 JSONL 中的同名记录（running 是最新实时状态）
    - 内存中的 completed 不覆盖 JSONL（JSONL 数据更完整）
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # 1. 先放 JSONL 记录（completed 数据更完整）
    merged: Dict[str, Dict] = {}
    for record in list_traces_from_jsonl(date, limit):
        merged[record["trace_id"]] = record

    # 2. 内存 running 覆盖 JSONL（running 是最新实时状态）
    for trace_id, meta in trace_collector.get_active_traces().items():
        if meta["status"] == "running":
            trace_meta = trace_collector.get_trace_meta(trace_id)
            merged[trace_id] = {
                "trace_id": trace_id,
                "command": meta["command"],
                "user_id": meta["user_id"],
                "group_id": trace_meta.group_id if trace_meta else None,
                "start_time": meta["start_time"],
                "duration_ms": None,
                "log_count": meta["log_count"],
                "status": "running",
            }

    # 3. 按 start_time 倒序（最近的在前）
    result = list(merged.values())
    result.sort(key=lambda x: x["start_time"], reverse=True)
    return {"status": 0, "msg": "ok", "data": result[:limit]}


# 注意：本路由必须声明在 `/api/traces/{trace_id}` **之前**，否则 FastAPI 会把
# "daily_counts" 当作 trace_id 匹配到详情路由（返回 404）。固定路径优先于路径参数。
@app.get("/api/traces/daily_counts")
async def get_trace_daily_counts(
    days: int = 60,
    _user: Dict = Depends(require_auth),
):
    """近 N 天每天的命令数——供前端日历选择器判断哪些日期可点击。

    Query 参数：
    - ``days``: 回溯天数，默认 60（约两个月），自动夹取到 [1, 366]。

    ``data`` 为按日期升序的列表，每项 ``{date, count}``；``count == 0`` 表示当天
    无命令记录、日历上不可点击。今天的计数实时可见（running 追踪已计入）。
    """
    days = max(1, min(days, 366))
    return {"status": 0, "msg": "ok", "data": daily_trace_counts(days)}


@app.get("/api/traces/{trace_id}")
async def get_trace_detail(
    trace_id: str,
    date: Optional[str] = None,
    _user: Dict = Depends(require_auth),
):
    """获取指定追踪的完整日志

    优先查内存；未命中时通过 trace_id 扫描 daily log 文件提取完整日志。
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

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

    # 未命中内存：先查 JSONL 目录确认元数据，再从 daily log 提取日志
    meta = get_trace_from_jsonl(trace_id, date)
    if meta is not None:
        logs = get_trace_logs_from_daily_log(trace_id, date)
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "trace_id": trace_id,
                "command": meta["command"],
                "user_id": meta["user_id"],
                "group_id": meta.get("group_id"),
                "bot_id": meta["bot_id"],
                "session_id": meta["session_id"],
                "start_time": meta["start_time"],
                "duration_ms": meta.get("duration_ms"),
                "log_count": meta.get("log_count"),
                "status": meta.get("status", "completed"),
                "logs": logs,
            },
        }

    return {"status": 404, "msg": "追踪不存在", "data": None}
