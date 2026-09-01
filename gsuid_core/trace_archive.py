import json
from typing import Dict, List, Optional, TypedDict
from pathlib import Path
from datetime import datetime, timedelta

from gsuid_core.logger import LOG_PATH, TraceContext
from gsuid_core.day_jsonl_store import (
    load_day_record,
    load_day_records,
    parse_jsonl_line,
    count_day_records,
    enqueue_day_jsonl,
    flush_day_jsonl_writes,
)


class CommandTraceListItem(TypedDict):
    trace_id: str
    command: str
    user_id: str
    group_id: str | None
    start_time: float
    duration_ms: int | None
    log_count: int
    status: str


class CommandTraceDayCount(TypedDict):
    date: str
    count: int


TRACE_JSONL_PATH = LOG_PATH / "traces"


def _root() -> Path:
    return TRACE_JSONL_PATH


def write_trace_meta(
    trace_id: str,
    meta: TraceContext,
    status: str,
    log_count: int,
    duration_ms: int | None = None,
) -> None:
    """写入追踪元数据（running 或 completed）。同 id 以最后一次为准。

    分片布局与 HTTP 共用 ``day_jsonl_store``（一条写线程）。旧整日 jsonl 仍可读。
    """
    record: Dict[str, object] = {
        "trace_id": trace_id,
        "command": meta.command,
        "user_id": meta.user_id,
        "group_id": meta.group_id,
        "bot_id": meta.bot_id,
        "session_id": meta.session_id,
        # 落盘墙钟时间戳（Unix 秒），供前端直接展示；perf_counter 单调时钟不可跨进程/展示
        "start_time": meta.start_ts,
        "status": status,
        "log_count": log_count,
    }
    if duration_ms is not None:
        record["duration_ms"] = duration_ms
    index_row = _list_row(record)
    if index_row is None:
        return
    enqueue_day_jsonl(
        _root(),
        trace_id,
        json.dumps(record, ensure_ascii=False) + "\n",
        json.dumps(index_row, ensure_ascii=False) + "\n",
    )


def _list_row(record: Dict[str, object]) -> Optional[CommandTraceListItem]:
    if "trace_id" not in record or "command" not in record or "user_id" not in record:
        return None
    if "start_time" not in record or "log_count" not in record:
        return None
    tid = record["trace_id"]
    command = record["command"]
    user_id = record["user_id"]
    start_time = record["start_time"]
    log_count = record["log_count"]
    if not isinstance(tid, str) or not isinstance(command, str) or not isinstance(user_id, str):
        return None
    if isinstance(log_count, bool) or not isinstance(log_count, int):
        return None
    if isinstance(start_time, bool) or not isinstance(start_time, (int, float)):
        return None
    status = record["status"] if "status" in record and isinstance(record["status"], str) else "completed"
    duration: int | None = None
    if "duration_ms" in record:
        raw_duration = record["duration_ms"]
        if isinstance(raw_duration, int) and not isinstance(raw_duration, bool):
            duration = raw_duration
    group_id: str | None = None
    if "group_id" in record:
        raw_group = record["group_id"]
        if isinstance(raw_group, str):
            group_id = raw_group
        elif raw_group is not None:
            return None
    return {
        "trace_id": tid,
        "command": command,
        "user_id": user_id,
        "group_id": group_id,
        "start_time": float(start_time),
        "duration_ms": duration,
        "log_count": log_count,
        "status": status,
    }


def get_trace_from_jsonl(trace_id: str, date_str: str | None = None) -> Optional[Dict[str, object]]:
    """查找单个追踪的最新元数据（shard，否则旧整日文件）。"""
    return load_day_record(_root(), trace_id, date_str)


def list_traces_from_jsonl(date_str: str | None = None, limit: int | None = None) -> List[CommandTraceListItem]:
    """指定日期目录列表（倒序）。同 id 只留最后一条。limit=None 为当天全量。"""
    rows: list[CommandTraceListItem] = []
    for record in load_day_records(_root(), date_str).values():
        row = _list_row(record)
        if row is None:
            continue
        rows.append(row)
    rows.sort(key=lambda x: x["start_time"], reverse=True)
    if limit is None:
        return rows
    return rows[:limit]


def count_traces_from_jsonl(date_str: str, *, flush: bool = True) -> int:
    """去重计数，口径与 ``list_traces_from_jsonl`` 一致。"""
    return count_day_records(_root(), date_str, flush=flush)


def daily_trace_counts(days: int = 60) -> List[CommandTraceDayCount]:
    """返回最近 ``days`` 天每天的去重命令数，按日期升序（最早在前）。

    供前端日历选择器判断可点击日期：``count == 0`` 的日期当天没有任何命令记录，
    不可点击。今天也计入——running 追踪在 ``start_trace`` 时即写入 JSONL running 标记，
    故当天计数实时可见，无需等命令结束。
    """
    flush_day_jsonl_writes()
    today = datetime.now().date()
    result: List[CommandTraceDayCount] = []
    for offset in range(days - 1, -1, -1):
        date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        result.append({"date": date_str, "count": count_traces_from_jsonl(date_str, flush=False)})
    return result


def get_trace_logs_from_daily_log(trace_id: str, date_str: str | None = None) -> List[Dict[str, str]]:
    """从 daily log 文件中按 trace_id 提取该追踪的完整日志列表。

    扫描 logs/YYYY-MM-DD.log 的每一行 JSON，匹配 trace_id 字段，
    返回该 trace 的所有日志条目（按时间顺序）。
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_PATH / f"{date_str}.log"
    if not log_file.exists():
        return []

    logs: List[Dict[str, str]] = []
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            record = parse_jsonl_line(line)
            if record is None:
                continue
            if "trace_id" not in record or record["trace_id"] != trace_id:
                continue
            timestamp = record["timestamp"] if "timestamp" in record else ""
            level = record["level"] if "level" in record else ""
            event = record["event"] if "event" in record else ""
            logs.append(
                {
                    "timestamp": str(timestamp) if timestamp is not None else "",
                    "level": str(level) if level is not None else "",
                    "event": str(event) if event is not None else "",
                }
            )
    return logs
