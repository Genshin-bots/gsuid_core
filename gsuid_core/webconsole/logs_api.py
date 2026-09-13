"""
Logs APIs
提供日志相关的 RESTful APIs
"""

from __future__ import annotations

import os
import re
import json
import time
import asyncio
import hashlib
import threading
from typing import Dict, List, TypeVar, Callable, Optional, TypedDict
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

from fastapi import Body, Query, Depends, Request
from pydantic import Field, BaseModel
from fastapi.responses import StreamingResponse

from gsuid_core.logger import (
    LOG_PATH,
    LogEntry,
    read_log,
    get_all_log_path,
    parse_history_logs_sync,
)
from gsuid_core.data_store import LOGS_CONFIG_PATH, error_mark_path
from gsuid_core.utils.path_safety import (
    PathEscapeError,
    safe_join,
    parse_iso_date,
    is_safe_filename,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.webconsole.session_store import SessionRecord

from ._api_tags import LOGS

LOG_LEVEL_VALUES: List[str] = [
    "trace",
    "debug",
    "info",
    "success",
    "warning",
    "error",
    "critical",
]

_LEVEL_MAPPING: dict[str, str] = {
    "info": "info",
    "warning": "warn",
    "warn": "warn",
    "error": "error",
    "debug": "debug",
    "critical": "error",
    "fatal": "error",
}

_LOGS_PER_PAGE_MAX = 200
_ERROR_REPORTS_PER_PAGE_MAX = 100
_ERROR_REPORT_MAX_BYTES = 2 * 1024 * 1024
_ERROR_EVENT_PREVIEW = 500
_ERROR_REPORT_NAME_RE = re.compile(r"^error_report_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d+\.json$")
_ERROR_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{32}$")
_ERROR_FINGERPRINT_LEN = 32
_ERROR_TIME_KEYS = frozenset({"timestamp", "_report_timestamp"})
_ERROR_INDEX_NAME = ".fingerprint_index.jsonl"
_ERROR_PARSE_YIELD_EVERY = 8
_ERROR_CACHE_LOCK = threading.Lock()
_ERROR_DISK_INDEX: dict[str, _ErrorFileMeta] | None = None
_ERROR_DISK_INDEX_ROOT: str | None = None
_ERROR_DISK_INDEX_DIRTY = False
_ERROR_PARSE_COUNT = 0
_ERROR_READ_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="error-report-read")
_T = TypeVar("_T")
_ERROR_LIST_INFLIGHT: dict[tuple[str, ...], asyncio.Future[ErrorReportListPage]] = {}
_ERROR_DETAIL_INFLIGHT: dict[tuple[str, ...], asyncio.Future[ErrorReportDetailResponse]] = {}
_ERROR_INFLIGHT_LOCK: asyncio.Lock | None = None


class LogsConfigData(TypedDict):
    visible_levels: list[str]


DEFAULT_LOGS_CONFIG: LogsConfigData = {
    "visible_levels": ["debug", "info", "warning", "error"],
}


class FormattedLogRow(TypedDict):
    id: int
    log_id: int
    date: str
    timestamp: str
    level: str
    source: str
    message: str
    details: None


class LogsPageData(TypedDict):
    count: int
    rows: list[FormattedLogRow]
    page: int
    per_page: int


class LogsListResponse(TypedDict):
    status: int
    msg: str
    data: LogsPageData | None


class LogsStatsData(TypedDict):
    total: int
    total_pages: int
    per_page: int
    info_count: int
    warn_count: int
    error_count: int
    debug_count: int


class LogsStatsResponse(TypedDict):
    status: int
    msg: str
    data: LogsStatsData


class ErrorReportListItem(TypedDict):
    id: str
    filename: str
    timestamp: str
    first_timestamp: str
    count: int
    level: str
    event: str
    pathname: str
    lineno: int | None
    size: int


class ErrorReportListPage(TypedDict):
    count: int
    rows: list[ErrorReportListItem]
    page: int
    per_page: int


class ErrorReportListResponse(TypedDict):
    status: int
    msg: str
    data: ErrorReportListPage | None


class ErrorReportOccurrence(TypedDict):
    filename: str
    timestamp: str


class ErrorReportDetailData(TypedDict):
    fingerprint: str
    count: int
    report: dict[str, object]
    occurrences: list[ErrorReportOccurrence]


class ErrorReportDetailResponse(TypedDict):
    status: int
    msg: str
    data: ErrorReportDetailData | None


@dataclass
class _ErrorFileMeta:
    mtime_ns: int
    size: int
    fingerprint: str
    filename: str
    timestamp: str
    level: str
    event: str
    pathname: str
    lineno: int | None


@dataclass
class _ErrorFileEntry:
    name: str
    path: Path
    mtime_ns: int
    size: int


class LogsConfigRequest(BaseModel):
    """日志控制台配置请求模型"""

    visible_levels: List[str] = Field(default_factory=list)


def _clamp_page(page: int) -> int:
    return 1 if page < 1 else page


def _clamp_per_page(per_page: int, cap: int) -> int:
    if per_page < 1:
        return 1
    if per_page > cap:
        return cap
    return per_page


def _sanitize_visible_levels(values: Optional[List[str]]) -> List[str]:
    """校验并清理 visible_levels：
    - 仅保留 LOG_LEVEL_VALUES 集合内的小写字符串
    - 保留用户提交顺序，去重
    - 允许空列表（表示用户主动全不选）
    """
    if not values:
        return []
    seen: List[str] = []
    seen_set: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        v = raw.strip().lower()
        if not v or v == "all":
            continue
        if v not in LOG_LEVEL_VALUES:
            continue
        if v in seen_set:
            continue
        seen.append(v)
        seen_set.add(v)
    return seen


def _merge_defaults(config: Optional[dict[str, object]]) -> LogsConfigData:
    """将存储中的旧配置与当前默认配置合并，确保响应体始终包含完整字段集"""
    if not isinstance(config, dict):
        return {"visible_levels": list(DEFAULT_LOGS_CONFIG["visible_levels"])}
    raw_levels = config["visible_levels"] if "visible_levels" in config else None
    levels = raw_levels if isinstance(raw_levels, list) else None
    str_levels: list[str] = [item for item in levels if isinstance(item, str)] if levels is not None else []
    return {"visible_levels": _sanitize_visible_levels(str_levels)}


def _as_str_object_dict(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, object] = {}
    for key, value in raw.items():
        if isinstance(key, str):
            out[key] = value
    return out


def load_logs_config() -> Optional[dict[str, object]]:
    """Load logs console config from file"""
    if LOGS_CONFIG_PATH.exists():
        try:
            with open(LOGS_CONFIG_PATH, "r", encoding="utf-8") as f:
                return _as_str_object_dict(json.load(f))
        except (OSError, json.JSONDecodeError):
            return None
    return None


def save_logs_config(config: LogsConfigData) -> bool:
    """Save logs console config to file"""
    try:
        LOGS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOGS_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        return True
    except OSError:
        return False


def _mapped_level(raw_level: str) -> str:
    key = raw_level.lower()
    return _LEVEL_MAPPING[key] if key in _LEVEL_MAPPING else "info"


def _entry_source(log: LogEntry) -> str:
    if "来源" in log:
        src = log["来源"]
        if isinstance(src, str) and src:
            return src
    return "core"


def _entry_message(log: LogEntry) -> str:
    message = log["内容"]
    if isinstance(message, str):
        return message
    return json.dumps(message, ensure_ascii=False)


def _load_dated_entries(day: str) -> list[tuple[str, LogEntry]]:
    log_file_path = LOG_PATH / f"{day}.log"
    if not log_file_path.is_file():
        return []
    logs = parse_history_logs_sync(log_file_path)
    return [(day, log) for log in logs]


def _load_range_entries(range_start: str, range_end: str) -> list[tuple[str, LogEntry]]:
    all_entries: list[tuple[str, LogEntry]] = []
    current_date = datetime.strptime(range_start, "%Y-%m-%d")
    end_date_obj = datetime.strptime(range_end, "%Y-%m-%d")
    while current_date <= end_date_obj:
        date_str = current_date.strftime("%Y-%m-%d")
        all_entries.extend(_load_dated_entries(date_str))
        current_date += timedelta(days=1)
    return all_entries


def _filter_entries(
    entries: list[tuple[str, LogEntry]],
    level: Optional[str],
    source: Optional[str],
    search: Optional[str],
) -> list[tuple[str, LogEntry]]:
    result = entries
    if level and level != "all":
        result = [(day, log) for day, log in result if _mapped_level(log["日志等级"]) == level]
    if source and source != "all":
        result = [(day, log) for day, log in result if _entry_source(log) == source]
    if search:
        search_lower = search.lower()
        filtered: list[tuple[str, LogEntry]] = []
        for day, log in result:
            if search_lower in _entry_message(log).lower():
                filtered.append((day, log))
        result = filtered
    return result


def _format_row(day: str, log: LogEntry, seq: int) -> FormattedLogRow:
    return {
        "id": seq,
        "log_id": log["id"],
        "date": day,
        "timestamp": log["时间"],
        "level": _mapped_level(log["日志等级"]),
        "source": "core",
        "message": _entry_message(log),
        "details": None,
    }


def _count_by_level(entries: list[tuple[str, LogEntry]]) -> tuple[int, int, int, int]:
    info_count = 0
    warn_count = 0
    error_count = 0
    debug_count = 0
    for _day, log in entries:
        mapped = _mapped_level(log["日志等级"])
        if mapped == "info":
            info_count += 1
        elif mapped == "warn":
            warn_count += 1
        elif mapped == "error":
            error_count += 1
        elif mapped == "debug":
            debug_count += 1
    return info_count, warn_count, error_count, debug_count


class _LogsQuery(TypedDict):
    range_start: str | None
    range_end: str | None
    day: str
    missing_single: bool


def _resolve_logs_query(
    date: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
) -> _LogsQuery | None:
    try:
        if start_date and end_date:
            range_start = parse_iso_date(start_date, default_today=False)
            range_end = parse_iso_date(end_date, default_today=False)
            return {
                "range_start": range_start,
                "range_end": range_end,
                "day": range_start,
                "missing_single": False,
            }
        day = parse_iso_date(date, default_today=True)
        log_file_path = LOG_PATH / f"{day}.log"
        return {
            "range_start": None,
            "range_end": None,
            "day": day,
            "missing_single": not log_file_path.exists(),
        }
    except PathEscapeError:
        return None


def get_logs_sync(
    date: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    level: Optional[str],
    source: Optional[str],
    search: Optional[str],
    page: int,
    per_page: int,
) -> LogsListResponse:
    query = _resolve_logs_query(date, start_date, end_date)
    if query is None:
        return {"status": 400, "msg": "非法日期", "data": None}

    page = _clamp_page(page)
    per_page = _clamp_per_page(per_page, _LOGS_PER_PAGE_MAX)

    range_start = query["range_start"]
    range_end = query["range_end"]
    if range_start is not None and range_end is not None:
        entries = _load_range_entries(range_start, range_end)
    else:
        if query["missing_single"]:
            return {"status": 404, "msg": "该日志不存在", "data": None}
        entries = _load_dated_entries(query["day"])

    entries = _filter_entries(entries, level, source, search)
    total = len(entries)
    start = (page - 1) * per_page
    page_entries = entries[start : start + per_page]
    rows = [_format_row(day, log, start + i + 1) for i, (day, log) in enumerate(page_entries)]
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "count": total,
            "rows": rows,
            "page": page,
            "per_page": per_page,
        },
    }


def get_log_stats_sync(
    date: Optional[str],
    start_date: Optional[str],
    end_date: Optional[str],
    level: Optional[str],
    source: Optional[str],
    search: Optional[str],
    per_page: int,
) -> LogsStatsResponse:
    empty: LogsStatsData = {
        "total": 0,
        "total_pages": 0,
        "per_page": per_page,
        "info_count": 0,
        "warn_count": 0,
        "error_count": 0,
        "debug_count": 0,
    }
    query = _resolve_logs_query(date, start_date, end_date)
    if query is None:
        return {"status": 0, "msg": "ok", "data": empty}

    per_page = _clamp_per_page(per_page, _LOGS_PER_PAGE_MAX)
    range_start = query["range_start"]
    range_end = query["range_end"]
    if range_start is not None and range_end is not None:
        entries = _load_range_entries(range_start, range_end)
    else:
        if query["missing_single"]:
            empty["per_page"] = per_page
            return {"status": 0, "msg": "ok", "data": empty}
        entries = _load_dated_entries(query["day"])

    info_count, warn_count, error_count, debug_count = _count_by_level(entries)
    filtered = _filter_entries(entries, level, source, search)
    total = len(filtered)
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


def _format_context_log(log: LogEntry, date: str) -> Dict[str, object]:
    return {
        "log_id": log["id"],
        "date": date,
        "timestamp": log["时间"],
        "level": _mapped_level(log["日志等级"]),
        "source": "core",
        "message": _entry_message(log),
    }


def get_log_context_sync(log_id: int, date: str, before: int, after: int) -> dict[str, object]:
    try:
        day = parse_iso_date(date, default_today=False)
    except PathEscapeError:
        return {"status": 404, "msg": "非法日期", "data": None}

    log_file_path = LOG_PATH / f"{day}.log"
    if not log_file_path.exists():
        return {"status": 404, "msg": "该日期的日志不存在", "data": None}

    log_files = parse_history_logs_sync(log_file_path)
    target_index = None
    for i, log in enumerate(log_files):
        if log["id"] == log_id:
            target_index = i
            break

    if target_index is None:
        return {"status": 404, "msg": "未找到指定的日志条目", "data": None}

    before_start = max(0, target_index - before)
    after_end = min(len(log_files), target_index + after + 1)
    before_logs = log_files[before_start:target_index]
    after_logs = log_files[target_index + 1 : after_end]
    target_log = log_files[target_index]
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "target": _format_context_log(target_log, day),
            "before_logs": [_format_context_log(log, day) for log in before_logs],
            "after_logs": [_format_context_log(log, day) for log in after_logs],
            "before_count": len(before_logs),
            "after_count": len(after_logs),
            "total_in_date": len(log_files),
            "has_more_before": before_start > 0,
            "has_more_after": after_end < len(log_files),
        },
    }


def _list_error_report_entries() -> list[_ErrorFileEntry]:
    if not error_mark_path.exists():
        return []
    entries: list[_ErrorFileEntry] = []
    with os.scandir(error_mark_path) as iterator:
        for item in iterator:
            if not _ERROR_REPORT_NAME_RE.match(item.name):
                continue
            if not item.is_file(follow_symlinks=False):
                continue
            stat = item.stat(follow_symlinks=False)
            entries.append(
                _ErrorFileEntry(
                    name=item.name,
                    path=Path(item.path),
                    mtime_ns=stat.st_mtime_ns,
                    size=stat.st_size,
                )
            )
    entries.sort(key=lambda item: item.name, reverse=True)
    return entries


def _error_report_day(filename: str) -> str | None:
    prefix = "error_report_"
    if not filename.startswith(prefix) or len(filename) < len(prefix) + 10:
        return None
    day = filename[len(prefix) : len(prefix) + 10]
    try:
        return parse_iso_date(day, default_today=False)
    except PathEscapeError:
        return None


def _filter_error_report_entries_by_date(
    entries: list[_ErrorFileEntry],
    date: str,
    start_date: str,
    end_date: str,
) -> list[_ErrorFileEntry]:
    range_start: str | None = None
    range_end: str | None = None
    try:
        if start_date.strip() and end_date.strip():
            range_start = parse_iso_date(start_date, default_today=False)
            range_end = parse_iso_date(end_date, default_today=False)
            if range_start > range_end:
                range_start, range_end = range_end, range_start
        elif date.strip():
            day = parse_iso_date(date, default_today=False)
            range_start, range_end = day, day
    except PathEscapeError:
        return []
    if range_start is None or range_end is None:
        return entries
    matched: list[_ErrorFileEntry] = []
    for entry in entries:
        day = _error_report_day(entry.name)
        if day is None:
            continue
        if range_start <= day <= range_end:
            matched.append(entry)
    return matched


def list_error_report_dates_sync() -> list[str]:
    days: set[str] = set()
    if not error_mark_path.exists():
        return []
    with os.scandir(error_mark_path) as iterator:
        for item in iterator:
            day = _error_report_day(item.name)
            if day is not None:
                days.add(day)
    return sorted(days, reverse=True)


def _json_str(raw: dict[str, object], key: str) -> str:
    if key not in raw:
        return ""
    value = raw[key]
    if isinstance(value, str):
        return value
    return str(value)


def _json_int(raw: dict[str, object], key: str) -> int | None:
    if key not in raw:
        return None
    value = raw[key]
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _fingerprint_of(raw: dict[str, object]) -> str:
    digest = hashlib.sha256()
    for key in sorted(raw):
        if key in _ERROR_TIME_KEYS:
            continue
        digest.update(key.encode("utf-8"))
        digest.update(b"\x1f")
        value = raw[key]
        if isinstance(value, str):
            digest.update(value.encode("utf-8", "replace"))
        elif isinstance(value, bool):
            digest.update(b"1" if value else b"0")
        elif isinstance(value, int):
            digest.update(str(value).encode("ascii"))
        elif value is None:
            digest.update(b"null")
        else:
            digest.update(
                json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
        digest.update(b"\x1e")
    return digest.hexdigest()[:_ERROR_FINGERPRINT_LEN]


def _error_index_path() -> Path:
    return error_mark_path / _ERROR_INDEX_NAME


def _load_disk_index() -> dict[str, _ErrorFileMeta]:
    global _ERROR_DISK_INDEX, _ERROR_DISK_INDEX_ROOT, _ERROR_DISK_INDEX_DIRTY
    root = str(error_mark_path)
    if _ERROR_DISK_INDEX is not None and _ERROR_DISK_INDEX_ROOT == root:
        return _ERROR_DISK_INDEX
    loaded: dict[str, _ErrorFileMeta] = {}
    index_path = _error_index_path()
    if index_path.is_file():
        try:
            with open(index_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    row = _as_str_object_dict(json.loads(stripped))
                    if row is None:
                        continue
                    name = _json_str(row, "name")
                    if not name:
                        continue
                    mtime_ns = 0
                    if "mtime_ns" in row and isinstance(row["mtime_ns"], int) and not isinstance(row["mtime_ns"], bool):
                        mtime_ns = row["mtime_ns"]
                    loaded[name] = _ErrorFileMeta(
                        mtime_ns=mtime_ns,
                        size=int(row["size"]) if "size" in row and isinstance(row["size"], int) else 0,
                        fingerprint=_json_str(row, "fingerprint"),
                        filename=name,
                        timestamp=_json_str(row, "timestamp"),
                        level=_json_str(row, "level") or "error",
                        event=_json_str(row, "event"),
                        pathname=_json_str(row, "pathname"),
                        lineno=_json_int(row, "lineno"),
                    )
        except (OSError, json.JSONDecodeError, ValueError):
            loaded = {}
    _ERROR_DISK_INDEX = loaded
    _ERROR_DISK_INDEX_ROOT = root
    _ERROR_DISK_INDEX_DIRTY = False
    return loaded


def _save_disk_index() -> None:
    global _ERROR_DISK_INDEX_DIRTY
    if not _ERROR_DISK_INDEX_DIRTY or _ERROR_DISK_INDEX is None:
        return
    index_path = _error_index_path()
    tmp_path = index_path.with_name(index_path.name + ".tmp")
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            for name, meta in _ERROR_DISK_INDEX.items():
                json.dump(
                    {
                        "name": name,
                        "mtime_ns": meta.mtime_ns,
                        "size": meta.size,
                        "fingerprint": meta.fingerprint,
                        "timestamp": meta.timestamp,
                        "level": meta.level,
                        "event": meta.event,
                        "pathname": meta.pathname,
                        "lineno": meta.lineno,
                    },
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.write("\n")
        tmp_path.replace(index_path)
        _ERROR_DISK_INDEX_DIRTY = False
    except OSError:
        if tmp_path.exists():
            tmp_path.unlink()


def _yield_gil() -> None:
    global _ERROR_PARSE_COUNT
    _ERROR_PARSE_COUNT += 1
    if _ERROR_PARSE_COUNT % _ERROR_PARSE_YIELD_EVERY == 0:
        time.sleep(0)


def _parse_error_file_meta(path: Path, mtime_ns: int, size: int) -> _ErrorFileMeta | None:
    if size > _ERROR_REPORT_MAX_BYTES:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = _as_str_object_dict(json.load(f))
    except (OSError, json.JSONDecodeError):
        return None
    if raw is None:
        return None
    timestamp = _json_str(raw, "_report_timestamp")
    if not timestamp:
        timestamp = path.stem.removeprefix("error_report_")
    level = _json_str(raw, "_log_level")
    if not level:
        level = _json_str(raw, "level")
    return _ErrorFileMeta(
        mtime_ns=mtime_ns,
        size=size,
        fingerprint=_fingerprint_of(raw),
        filename=path.name,
        timestamp=timestamp,
        level=level.lower() if level else "error",
        event=_truncate(_json_str(raw, "event"), _ERROR_EVENT_PREVIEW),
        pathname=_json_str(raw, "pathname"),
        lineno=_json_int(raw, "lineno"),
    )


def _load_error_file_meta(entry: _ErrorFileEntry) -> _ErrorFileMeta | None:
    global _ERROR_DISK_INDEX_DIRTY
    index = _load_disk_index()
    cached = index[entry.name] if entry.name in index else None
    if cached is not None and cached.mtime_ns == entry.mtime_ns and cached.size == entry.size:
        return cached
    meta = _parse_error_file_meta(entry.path, entry.mtime_ns, entry.size)
    _yield_gil()
    if meta is None:
        return None
    with _ERROR_CACHE_LOCK:
        index[entry.name] = meta
        _ERROR_DISK_INDEX_DIRTY = True
    if _ERROR_PARSE_COUNT % 256 == 0:
        _save_disk_index()
    return meta


def _collect_error_groups(entries: list[_ErrorFileEntry]) -> list[list[_ErrorFileMeta]]:
    groups: dict[str, list[_ErrorFileMeta]] = {}
    order: list[str] = []
    for entry in entries:
        meta = _load_error_file_meta(entry)
        if meta is None:
            continue
        if meta.fingerprint not in groups:
            groups[meta.fingerprint] = []
            order.append(meta.fingerprint)
        groups[meta.fingerprint].append(meta)
    _save_disk_index()
    return [groups[fp] for fp in order]


def _group_to_list_item(members: list[_ErrorFileMeta]) -> ErrorReportListItem:
    latest = members[0]
    oldest = members[-1]
    return {
        "id": latest.fingerprint,
        "filename": latest.filename,
        "timestamp": latest.timestamp,
        "first_timestamp": oldest.timestamp,
        "count": len(members),
        "level": latest.level,
        "event": latest.event,
        "pathname": latest.pathname,
        "lineno": latest.lineno,
        "size": latest.size,
    }


def list_error_reports_sync(
    page: int,
    per_page: int,
    search: str,
    level: str,
    date: str = "",
    start_date: str = "",
    end_date: str = "",
) -> ErrorReportListPage:
    page = _clamp_page(page)
    per_page = _clamp_per_page(per_page, _ERROR_REPORTS_PER_PAGE_MAX)
    entries = _filter_error_report_entries_by_date(
        _list_error_report_entries(),
        date,
        start_date,
        end_date,
    )
    groups = _collect_error_groups(entries)
    search_lower = search.strip().lower()
    level_lower = level.strip().lower()
    matched: list[ErrorReportListItem] = []
    for members in groups:
        item = _group_to_list_item(members)
        if level_lower and level_lower != "all" and item["level"] != level_lower:
            continue
        if search_lower:
            hay = f"{item['event']} {item['pathname']} {item['filename']}".lower()
            if search_lower not in hay:
                continue
        matched.append(item)
    total = len(matched)
    start = (page - 1) * per_page
    return {
        "count": total,
        "rows": matched[start : start + per_page],
        "page": page,
        "per_page": per_page,
    }


def _read_error_payload(path: Path) -> dict[str, object] | None:
    try:
        size = path.stat().st_size
        if size > _ERROR_REPORT_MAX_BYTES:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return _as_str_object_dict(json.load(f))
    except (OSError, json.JSONDecodeError):
        return None


def _resolve_error_fingerprint(report_id: str) -> str | None:
    if _ERROR_FINGERPRINT_RE.match(report_id):
        return report_id
    if not is_safe_filename(report_id) or not _ERROR_REPORT_NAME_RE.match(report_id):
        return None
    try:
        path = safe_join(error_mark_path, report_id)
        stat = path.stat()
    except (PathEscapeError, OSError):
        return None
    meta = _load_error_file_meta(
        _ErrorFileEntry(name=path.name, path=path, mtime_ns=stat.st_mtime_ns, size=stat.st_size)
    )
    if meta is None:
        return None
    return meta.fingerprint


def read_error_report_sync(
    report_id: str,
    date: str = "",
    start_date: str = "",
    end_date: str = "",
) -> ErrorReportDetailResponse:
    fingerprint = _resolve_error_fingerprint(report_id)
    if fingerprint is None:
        return {"status": 400, "msg": "非法文件名", "data": None}
    entries = _filter_error_report_entries_by_date(
        _list_error_report_entries(),
        date,
        start_date,
        end_date,
    )
    index = _load_disk_index()
    members: list[_ErrorFileMeta] = []
    missing: list[_ErrorFileEntry] = []
    for entry in entries:
        if entry.name in index:
            cached = index[entry.name]
            if cached.mtime_ns == entry.mtime_ns and cached.size == entry.size:
                if cached.fingerprint == fingerprint:
                    members.append(cached)
                continue
        missing.append(entry)
    for entry in missing:
        meta = _load_error_file_meta(entry)
        if meta is not None and meta.fingerprint == fingerprint:
            members.append(meta)
    if missing:
        _save_disk_index()
    members.sort(key=lambda item: item.filename, reverse=True)
    if not members:
        groups = _collect_error_groups(entries)
        for group in groups:
            if group and group[0].fingerprint == fingerprint:
                members = group
                break
    if not members:
        return {"status": 404, "msg": "错误报告不存在", "data": None}
    latest = members[0]
    try:
        payload_path = safe_join(error_mark_path, latest.filename)
    except PathEscapeError:
        return {"status": 400, "msg": "非法文件名", "data": None}
    payload = _read_error_payload(payload_path)
    if payload is None:
        return {"status": 1, "msg": "读取失败", "data": None}
    occurrences: list[ErrorReportOccurrence] = [
        {"filename": meta.filename, "timestamp": meta.timestamp} for meta in members
    ]
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "fingerprint": fingerprint,
            "count": len(members),
            "report": payload,
            "occurrences": occurrences,
        },
    }


@app.get("/api/logs", summary="获取日志列表", tags=LOGS)
async def get_logs(
    request: Request,
    date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    level: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    _user: SessionRecord = Depends(require_auth),
) -> LogsListResponse:
    """获取日志列表。解析与过滤在线程池中完成，避免卡住事件循环。"""
    return await asyncio.to_thread(
        get_logs_sync,
        date,
        start_date,
        end_date,
        level,
        source,
        search,
        page,
        per_page,
    )


@app.get("/api/logs/available-dates", summary="获取可用日期列表", tags=LOGS)
async def get_available_log_dates(
    _user: SessionRecord = Depends(require_auth),
):
    """
    获取所有存在日志文件的日期列表，用于前端日历选择器标记可选择的日期
    """
    log_files = await asyncio.to_thread(get_all_log_path)
    available_dates = [file.stem for file in log_files]
    available_dates.sort(reverse=True)
    return {"status": 0, "msg": "ok", "data": available_dates}


@app.get("/api/logs/sources", summary="获取日志来源", tags=LOGS)
async def get_log_sources(request: Request, _user: SessionRecord = Depends(require_auth)):
    """
    获取可用的日志来源列表
    """
    return {
        "status": 0,
        "msg": "ok",
        "data": ["api", "auth", "database", "scheduler", "core"],
    }


@app.get("/api/logs/stats", summary="获取日志统计", tags=LOGS)
async def get_log_stats(
    request: Request,
    date: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    level: Optional[str] = None,
    source: Optional[str] = None,
    search: Optional[str] = None,
    per_page: int = 100,
    _user: SessionRecord = Depends(require_auth),
) -> LogsStatsResponse:
    """获取日志统计信息。与 /api/logs 共享解析缓存。"""
    return await asyncio.to_thread(
        get_log_stats_sync,
        date,
        start_date,
        end_date,
        level,
        source,
        search,
        per_page,
    )


@app.get("/api/logs/context", summary="获取日志上下文", tags=LOGS)
async def get_log_context(
    request: Request,
    log_id: int,
    date: str,
    before: int = 10,
    after: int = 10,
    _user: SessionRecord = Depends(require_auth),
):
    """获取指定日志前后的上下文日志。"""
    before = min(max(before, 0), 100)
    after = min(max(after, 0), 100)
    return await asyncio.to_thread(get_log_context_sync, log_id, date, before, after)


def _error_inflight_lock() -> asyncio.Lock:
    global _ERROR_INFLIGHT_LOCK
    if _ERROR_INFLIGHT_LOCK is None:
        _ERROR_INFLIGHT_LOCK = asyncio.Lock()
    return _ERROR_INFLIGHT_LOCK


async def _coalesced_error_call(
    table: dict[tuple[str, ...], asyncio.Future[_T]],
    key: tuple[str, ...],
    thunk: Callable[[], _T],
) -> _T:
    loop = asyncio.get_running_loop()
    lock = _error_inflight_lock()
    async with lock:
        if key in table:
            fut = table[key]
        else:
            fut = loop.run_in_executor(_ERROR_READ_EXECUTOR, thunk)
            table[key] = fut
    try:
        return await fut
    finally:
        async with lock:
            if key in table and table[key] is fut:
                del table[key]


def _error_index_is_warm() -> bool:
    return _ERROR_DISK_INDEX is not None and _ERROR_DISK_INDEX_ROOT == str(error_mark_path)


def _warm_error_report_index() -> None:
    if _error_index_is_warm():
        return
    _collect_error_groups(_list_error_report_entries())


def _shutdown_error_report_reads() -> None:
    _ERROR_READ_EXECUTOR.shutdown(wait=False, cancel_futures=True)


try:
    from gsuid_core.server import on_core_shutdown

    on_core_shutdown(_shutdown_error_report_reads)
except ImportError:
    pass


@app.get("/api/logs/error-reports", summary="获取错误报告列表", tags=LOGS)
async def list_error_reports(
    request: Request,
    page: int = 1,
    per_page: int = 50,
    search: str = "",
    level: str = "",
    date: str = "",
    start_date: str = "",
    end_date: str = "",
    _user: SessionRecord = Depends(require_auth),
) -> ErrorReportListResponse:
    """分页列出合并后的错误报告。磁盘索引 + 单 worker，避免 json.loads 占满 GIL。"""
    data = await _coalesced_error_call(
        _ERROR_LIST_INFLIGHT,
        ("list", str(page), str(per_page), search, level, date, start_date, end_date),
        lambda: list_error_reports_sync(page, per_page, search, level, date, start_date, end_date),
    )
    return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/logs/error-reports/available-dates", summary="错误报告可用日期", tags=LOGS)
async def list_error_report_dates(
    request: Request,
    _user: SessionRecord = Depends(require_auth),
):
    """从文件名提取 YYYY-MM-DD，不读取 JSON 内容。"""
    dates = await asyncio.to_thread(list_error_report_dates_sync)
    loop = asyncio.get_running_loop()
    loop.run_in_executor(_ERROR_READ_EXECUTOR, _warm_error_report_index)
    return {"status": 0, "msg": "ok", "data": dates}


@app.get("/api/logs/error-reports/{filename}", summary="获取错误报告详情", tags=LOGS)
async def get_error_report(
    request: Request,
    filename: str,
    date: str = "",
    start_date: str = "",
    end_date: str = "",
    _user: SessionRecord = Depends(require_auth),
) -> ErrorReportDetailResponse:
    """按内容指纹合并后的详情：代表 JSON + 当前筛选范围内全部出现时间。"""
    return await _coalesced_error_call(
        _ERROR_DETAIL_INFLIGHT,
        ("detail", filename, date, start_date, end_date),
        lambda: read_error_report_sync(filename, date, start_date, end_date),
    )


@app.get("/api/logs/stream", summary="实时日志流", tags=LOGS)
async def stream_logs(
    request: Request,
    level: Optional[List[str]] = Query(default=["DEBUG", "INFO", "ERROR"]),
    last_event_id: Optional[str] = Query(default=None, description="断点续传：上次收到的 SSE id"),
    _user: SessionRecord = Depends(require_auth),
):
    """Stream real-time logs using Server-Sent Events

    Args:
        level: 允许推送的日志级别列表，如 ["DEBUG", "INFO", "ERROR"]。
               默认为 ["DEBUG", "INFO", "ERROR"]；传 ["all"] 时推送全部级别日志。
               支持重复参数，如 ?level=DEBUG&level=INFO&level=ERROR。
        last_event_id: 上次收到的 SSE ``id:``，从该序号之后续传，不传则回放整个缓冲。
    """
    if level and "all" in [ld.lower() for ld in level]:
        level = None
    resume_from = request.headers.get("last-event-id") or last_event_id
    return StreamingResponse(
        read_log(levels=level, last_event_id=resume_from),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/logs/levels", summary="获取可用日志级别", tags=LOGS)
async def get_log_levels(_user: SessionRecord = Depends(require_auth)):
    """获取可用的日志级别列表（供前端实时日志级别切换使用）"""
    return {
        "status": 0,
        "msg": "ok",
        "data": [
            {"label": "全部", "value": "all"},
            {"label": "TRACE", "value": "trace"},
            {"label": "DEBUG", "value": "debug"},
            {"label": "INFO", "value": "info"},
            {"label": "SUCCESS", "value": "success"},
            {"label": "WARNING", "value": "warning"},
            {"label": "ERROR", "value": "error"},
            {"label": "CRITICAL", "value": "critical"},
        ],
    }


@app.get("/api/logs/config", summary="获取日志控制台配置", tags=LOGS)
async def get_logs_config(
    request: Request,
    _user: SessionRecord = Depends(require_auth),
):
    """获取用户保存的日志控制台配置（供前端持久化级别选择偏好使用）"""
    config = await asyncio.to_thread(load_logs_config)
    return {
        "status": 0,
        "msg": "ok",
        "data": _merge_defaults(config),
    }


@app.put("/api/logs/config", summary="保存日志控制台配置", tags=LOGS)
async def save_logs_config_endpoint(
    request: Request,
    body: LogsConfigRequest = Body(default=LogsConfigRequest()),
    _user: SessionRecord = Depends(require_auth),
):
    """保存用户日志控制台配置"""
    sanitized = _sanitize_visible_levels(body.visible_levels)
    new_config: LogsConfigData = {"visible_levels": sanitized}
    ok = await asyncio.to_thread(save_logs_config, new_config)
    if ok:
        return {"status": 0, "msg": "saved", "data": new_config}
    return {"status": 1, "msg": "保存失败", "data": None}
