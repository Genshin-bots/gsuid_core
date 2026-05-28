import json
from typing import Dict, List, Optional
from pathlib import Path
from datetime import datetime

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
        "start_time": meta.start_time,
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
