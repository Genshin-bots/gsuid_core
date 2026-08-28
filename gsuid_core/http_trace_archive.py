"""HTTP 请求追踪 JSONL 元数据与 daily log 扫描。

读路径不加锁；写路径仅在一行 append 上持 threading.Lock。
"""

from __future__ import annotations

import json
import threading
from typing import Dict, List, Optional, TypedDict
from pathlib import Path
from datetime import datetime, timedelta

from gsuid_core.models import (
    HttpTraceLife,
    HttpTraceDetail,
    HttpTraceContext,
    HttpTraceLogLine,
    HttpTraceListItem,
    HttpTraceJsonlRecord,
)
from gsuid_core.utils.path_safety import parse_iso_date

_WRITE_LOCK = threading.Lock()
_CORE_PLUGIN = "SayuCore"


class HttpTraceDayCount(TypedDict):
    date: str
    count: int


def _log_path() -> Path:
    from gsuid_core.logger import LOG_PATH

    return LOG_PATH


def _jsonl_path(date_str: str | None = None) -> Path:
    date_str = parse_iso_date(date_str, default_today=True)
    folder = _log_path() / "http_traces"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{date_str}.jsonl"


def write_http_trace_meta(
    meta: HttpTraceContext,
    status: HttpTraceLife,
    log_count: int,
    duration_ms: int | None = None,
    status_code: int | None = None,
    error_count: int | None = None,
) -> None:
    """写入 HTTP 追踪元数据。同 uuid 可多次写，读时取最后一行。"""
    record: HttpTraceJsonlRecord = {
        "trace_id": meta.trace_id,
        "method": meta.method,
        "path": meta.path,
        "query_redacted": meta.query_redacted,
        "client_ip": meta.client_ip,
        "user_id": meta.user_id,
        "user_name": meta.user_name,
        "client_request_id": meta.client_request_id,
        "content_length": meta.content_length,
        "start_time": meta.start_ts,
        "status": status,
        "log_count": log_count,
    }
    if duration_ms is not None:
        record["duration_ms"] = duration_ms
    if status_code is not None:
        record["status_code"] = status_code
    if error_count is not None:
        record["error_count"] = error_count
    if meta.response_content_type is not None:
        record["response_content_type"] = meta.response_content_type
    if meta.response_preview is not None:
        record["response_preview"] = meta.response_preview

    line = json.dumps(record, ensure_ascii=False) + "\n"
    jsonl_path = _jsonl_path()
    with _WRITE_LOCK:
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(line)


def _parse_jsonl_line(line: str) -> Optional[Dict[str, object]]:
    text = line.strip()
    if not text:
        return None
    try:
        record = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    return record


def _optional_int(record: Dict[str, object], key: str) -> Optional[int]:
    if key not in record:
        return None
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_str(record: Dict[str, object], key: str) -> Optional[str]:
    if key not in record:
        return None
    value = record[key]
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return value


def _required_str(record: Dict[str, object], key: str) -> Optional[str]:
    if key not in record:
        return None
    value = record[key]
    if not isinstance(value, str) or not value:
        return None
    return value


def _jsonl_to_list_item(record: Dict[str, object]) -> Optional[HttpTraceListItem]:
    trace_id = _required_str(record, "trace_id")
    method = _required_str(record, "method")
    path = _required_str(record, "path")
    if trace_id is None or method is None or path is None:
        return None
    status_raw = _required_str(record, "status")
    status: HttpTraceLife = "completed"
    if status_raw == "running":
        status = "running"
    query_redacted = _optional_str(record, "query_redacted")
    client_ip = _optional_str(record, "client_ip")
    log_count = _optional_int(record, "log_count")
    start_time = record["start_time"] if "start_time" in record else None
    if not isinstance(start_time, (int, float)):
        return None
    error_count = _optional_int(record, "error_count")
    return {
        "trace_id": trace_id,
        "method": method,
        "path": path,
        "query_redacted": query_redacted if query_redacted is not None else "",
        "client_ip": client_ip if client_ip is not None else "",
        "user_id": _optional_str(record, "user_id"),
        "user_name": _optional_str(record, "user_name"),
        "start_time": float(start_time),
        "duration_ms": _optional_int(record, "duration_ms"),
        "log_count": log_count if log_count is not None else 0,
        "error_count": error_count if error_count is not None else 0,
        "status_code": _optional_int(record, "status_code"),
        "status": status,
    }


def get_http_trace_from_jsonl(trace_id: str, date_str: str | None = None) -> Optional[HttpTraceJsonlRecord]:
    jsonl_path = _jsonl_path(date_str)
    if not jsonl_path.exists():
        return None
    result: Optional[HttpTraceJsonlRecord] = None
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            record = _parse_jsonl_line(line)
            if record is None:
                continue
            if "trace_id" not in record:
                continue
            if record["trace_id"] != trace_id:
                continue
            item = _jsonl_to_list_item(record)
            if item is None:
                continue
            packed: HttpTraceJsonlRecord = {
                "trace_id": item["trace_id"],
                "method": item["method"],
                "path": item["path"],
                "query_redacted": item["query_redacted"],
                "client_ip": item["client_ip"],
                "user_id": item["user_id"],
                "user_name": item["user_name"],
                "client_request_id": _optional_str(record, "client_request_id"),
                "content_length": _optional_int(record, "content_length"),
                "start_time": item["start_time"],
                "status": item["status"],
                "log_count": item["log_count"],
            }
            duration_ms = item["duration_ms"]
            if duration_ms is not None:
                packed["duration_ms"] = duration_ms
            status_code = item["status_code"]
            if status_code is not None:
                packed["status_code"] = status_code
            packed["error_count"] = item["error_count"]
            ct = _optional_str(record, "response_content_type")
            if ct is not None:
                packed["response_content_type"] = ct
            preview = _optional_str(record, "response_preview")
            if preview is not None:
                packed["response_preview"] = preview
            result = packed
    return result


def list_http_traces_from_jsonl(date_str: str | None = None) -> List[HttpTraceListItem]:
    """读指定日期 JSONL；同 uuid 只留最后一行。不加 limit，过滤由 API 做。"""
    jsonl_path = _jsonl_path(date_str)
    if not jsonl_path.exists():
        return []
    seen: Dict[str, HttpTraceListItem] = {}
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            record = _parse_jsonl_line(line)
            if record is None:
                continue
            item = _jsonl_to_list_item(record)
            if item is None:
                continue
            seen[item["trace_id"]] = item
    records = list(seen.values())
    records.sort(key=lambda x: x["start_time"], reverse=True)
    return records


def count_http_traces_from_jsonl(date_str: str) -> int:
    jsonl_path = _jsonl_path(date_str)
    if not jsonl_path.exists():
        return 0
    seen: set[str] = set()
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            record = _parse_jsonl_line(line)
            if record is None:
                continue
            tid = _required_str(record, "trace_id")
            if tid:
                seen.add(tid)
    return len(seen)


def daily_http_trace_counts(days: int = 60) -> List[HttpTraceDayCount]:
    today = datetime.now().date()
    result: List[HttpTraceDayCount] = []
    for offset in range(days - 1, -1, -1):
        date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        result.append({"date": date_str, "count": count_http_traces_from_jsonl(date_str)})
    return result


def get_http_trace_logs_from_daily_log(trace_id: str, date_str: str | None = None) -> List[HttpTraceLogLine]:
    """扫描 daily log；只认字段 http_trace_id，禁止抄命令的 trace_id。"""
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")
    log_file = _log_path() / f"{date_str}.log"
    if not log_file.exists():
        return []
    logs: List[HttpTraceLogLine] = []
    with open(log_file, "r", encoding="utf-8") as f:
        for line in f:
            record = _parse_jsonl_line(line)
            if record is None:
                continue
            if "http_trace_id" not in record:
                continue
            if record["http_trace_id"] != trace_id:
                continue
            timestamp = record["timestamp"] if "timestamp" in record else ""
            level = record["level"] if "level" in record else ""
            event = record["event"] if "event" in record else ""
            plugin = _optional_str(record, "plugin")
            logs.append(
                {
                    "timestamp": str(timestamp) if timestamp is not None else "",
                    "level": str(level) if level is not None else "",
                    "event": str(event) if event is not None else "",
                    "plugin": plugin if plugin else _CORE_PLUGIN,
                }
            )
    return logs


def jsonl_record_to_detail(record: HttpTraceJsonlRecord, logs: List[HttpTraceLogLine]) -> HttpTraceDetail:
    duration = record["duration_ms"] if "duration_ms" in record else None
    status_code = record["status_code"] if "status_code" in record else None
    error_count = record["error_count"] if "error_count" in record else 0
    return {
        "trace_id": record["trace_id"],
        "method": record["method"],
        "path": record["path"],
        "query_redacted": record["query_redacted"],
        "client_ip": record["client_ip"],
        "user_id": record["user_id"],
        "user_name": record["user_name"],
        "start_time": record["start_time"],
        "duration_ms": duration,
        "log_count": record["log_count"],
        "error_count": error_count if error_count is not None else 0,
        "status_code": status_code,
        "status": record["status"],
        "client_request_id": record["client_request_id"],
        "content_length": record["content_length"],
        "response_content_type": record["response_content_type"] if "response_content_type" in record else None,
        "response_preview": record["response_preview"] if "response_preview" in record else None,
        "logs": logs,
    }
