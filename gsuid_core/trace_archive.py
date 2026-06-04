import json
from typing import Dict, List, Optional
from pathlib import Path
from datetime import datetime, timedelta

from gsuid_core.logger import LOG_PATH, TraceContext

TRACE_JSONL_PATH = LOG_PATH / "traces"


def _get_jsonl_path(date_str: str | None = None) -> Path:
    """按日期获取 JSONL 路径，格式：logs/traces/YYYY-MM-DD.jsonl"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    TRACE_JSONL_PATH.mkdir(parents=True, exist_ok=True)
    return TRACE_JSONL_PATH / f"{date_str}.jsonl"


def write_trace_meta(
    trace_id: str,
    meta: TraceContext,
    status: str,
    log_count: int,
    duration_ms: int | None = None,
) -> None:
    """写入追踪元数据到 JSONL（running 或 completed）。

    同 trace_id 可多次写入，以最后一次状态为准。
    """
    jsonl_path = _get_jsonl_path()

    record: Dict = {
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

    with open(jsonl_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def get_trace_from_jsonl(trace_id: str, date_str: str | None = None) -> Optional[Dict]:
    """从 JSONL 倒序扫描查找单个追踪的最新元数据。

    同一 trace_id 可能有多条记录（running -> completed），取最后一条。
    """
    jsonl_path = _get_jsonl_path(date_str)
    if not jsonl_path.exists():
        return None

    result: Optional[Dict] = None
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("trace_id") == trace_id:
                    result = record
            except json.JSONDecodeError:
                continue
    return result


def list_traces_from_jsonl(date_str: str | None = None, limit: int = 500) -> List[Dict]:
    """从 JSONL 读取指定日期的追踪目录列表（倒序，最近更新的在前）。

    同 trace_id 只保留最新状态记录。
    """
    jsonl_path = _get_jsonl_path(date_str)
    if not jsonl_path.exists():
        return []

    seen: Dict[str, Dict] = {}
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                tid = record["trace_id"]
                seen[tid] = {
                    "trace_id": tid,
                    "command": record["command"],
                    "user_id": record["user_id"],
                    "group_id": record.get("group_id"),
                    "start_time": record["start_time"],
                    "duration_ms": record.get("duration_ms"),
                    "log_count": record["log_count"],
                    "status": record.get("status", "completed"),
                }
            except (json.JSONDecodeError, KeyError):
                continue

    records = list(seen.values())
    records.sort(key=lambda x: x["start_time"], reverse=True)
    return records[:limit]


def count_traces_from_jsonl(date_str: str) -> int:
    """统计某天 JSONL 中去重后的命令追踪数。

    同一 trace_id 一天内会写多条（running -> completed），按 trace_id 去重计数，
    口径与 ``list_traces_from_jsonl`` 一致，且不受其 ``limit`` 截断影响。
    文件不存在（当天无任何命令）时返回 0。
    """
    jsonl_path = _get_jsonl_path(date_str)
    if not jsonl_path.exists():
        return 0

    seen: set[str] = set()
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                tid = json.loads(line).get("trace_id")
            except json.JSONDecodeError:
                continue
            if tid:
                seen.add(tid)
    return len(seen)


def daily_trace_counts(days: int = 60) -> List[Dict]:
    """返回最近 ``days`` 天每天的去重命令数，按日期升序（最早在前）。

    供前端日历选择器判断可点击日期：``count == 0`` 的日期当天没有任何命令记录，
    不可点击。今天也计入——running 追踪在 ``start_trace`` 时即写入 JSONL running 标记，
    故当天计数实时可见，无需等命令结束。
    """
    today = datetime.now().date()
    result: List[Dict] = []
    for offset in range(days - 1, -1, -1):
        date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        result.append({"date": date_str, "count": count_traces_from_jsonl(date_str)})
    return result


def get_trace_logs_from_daily_log(trace_id: str, date_str: str | None = None) -> List[Dict]:
    """从 daily log 文件中按 trace_id 提取该追踪的完整日志列表。

    扫描 logs/YYYY-MM-DD.log 的每一行 JSON，匹配 trace_id 字段，
    返回该 trace 的所有日志条目（按时间顺序）。
    """
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_PATH / f"{date_str}.log"
    if not log_file.exists():
        return []

    logs: List[Dict] = []
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get("trace_id") == trace_id:
                    logs.append(
                        {
                            "timestamp": record.get("timestamp", ""),
                            "level": record.get("level", ""),
                            "event": record.get("event", ""),
                        }
                    )
            except json.JSONDecodeError:
                continue
    return logs
