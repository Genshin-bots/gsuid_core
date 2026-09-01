"""HTTP 请求追踪落盘与扫描。分片布局见 ``day_jsonl_store``（与命令追踪共用写线程）。"""

from __future__ import annotations

import json
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
from gsuid_core.day_jsonl_store import (
    shard_key,
    load_day_record,
    load_day_records,
    parse_jsonl_line,
    count_day_records,
    enqueue_day_jsonl,
    flush_day_jsonl_writes,
)

_CORE_PLUGIN = "SayuCore"
_shard_key = shard_key
flush_http_trace_writes = flush_day_jsonl_writes


class HttpTraceDayCount(TypedDict):
    date: str
    count: int


def _log_path() -> Path:
    from gsuid_core.logger import LOG_PATH

    return LOG_PATH


def _traces_root() -> Path:
    return _log_path() / "http_traces"


def write_http_trace_meta(
    meta: HttpTraceContext,
    status: HttpTraceLife,
    log_count: int,
    duration_ms: int | None = None,
    status_code: int | None = None,
    error_count: int | None = None,
) -> None:
    """写入 HTTP 追踪元数据。同 uuid 可多次写，读时取最后一行。"""
    if status == "running":
        return
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

    item = _jsonl_to_list_item(dict(record))
    if item is None:
        return
    enqueue_day_jsonl(
        _traces_root(),
        meta.trace_id,
        json.dumps(record, ensure_ascii=False) + "\n",
        json.dumps(item, ensure_ascii=False) + "\n",
    )


_parse_jsonl_line = parse_jsonl_line


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


def _pack_full_record(record: Dict[str, object], item: HttpTraceListItem) -> HttpTraceJsonlRecord:
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
    return packed


def get_http_trace_from_jsonl(trace_id: str, date_str: str | None = None) -> Optional[HttpTraceJsonlRecord]:
    record = load_day_record(_traces_root(), trace_id, date_str)
    if record is None:
        return None
    item = _jsonl_to_list_item(record)
    if item is None:
        return None
    return _pack_full_record(record, item)


def list_http_traces_from_jsonl(date_str: str | None = None) -> List[HttpTraceListItem]:
    """读指定日期；同 uuid 只留最后一行。不加 limit，过滤由 API 做。"""
    items: list[HttpTraceListItem] = []
    for record in load_day_records(_traces_root(), date_str).values():
        item = _jsonl_to_list_item(record)
        if item is None:
            continue
        items.append(item)
    items.sort(key=lambda x: x["start_time"], reverse=True)
    return items


def count_http_traces_from_jsonl(date_str: str, *, flush: bool = True) -> int:
    return count_day_records(_traces_root(), date_str, flush=flush)


def daily_http_trace_counts(days: int = 60) -> List[HttpTraceDayCount]:
    flush_day_jsonl_writes()
    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    result: List[HttpTraceDayCount] = []
    for offset in range(days - 1, -1, -1):
        date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        n = count_http_traces_from_jsonl(date_str, flush=False)
        if date_str == today_str:
            from gsuid_core.logger import http_trace_collector

            n += len(http_trace_collector.get_active_traces())
        result.append({"date": date_str, "count": n})
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
