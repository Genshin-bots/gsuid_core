"""HTTP `/api` 请求追踪 REST：列表 / 日历 / 详情。自身路径不进入追踪。"""

from __future__ import annotations

import asyncio
from typing import List, Literal, Optional, TypedDict

from fastapi import Query, Depends

from gsuid_core.logger import http_trace_collector
from gsuid_core.models import HttpTraceDetail, HttpTraceListItem
from gsuid_core.utils.path_safety import PathEscapeError, parse_iso_date
from gsuid_core.http_trace_archive import (
    HttpTraceDayCount,
    jsonl_record_to_detail,
    daily_http_trace_counts,
    get_http_trace_from_jsonl,
    list_http_traces_from_jsonl,
    get_http_trace_logs_from_daily_log,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.webconsole.session_store import SessionRecord

from ._api_tags import HTTP_TRACE

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})
_STATUS_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx"})
_TRUE_TOKENS = frozenset({"1", "true", "yes"})


class HttpTraceListPage(TypedDict):
    rows: List[HttpTraceListItem]
    count: int
    page: int
    per_page: int


class HttpTraceListResponse(TypedDict):
    status: int
    msg: str
    data: HttpTraceListPage


class HttpTraceDetailResponse(TypedDict):
    status: int
    msg: str
    data: Optional[HttpTraceDetail]


class HttpTraceCountsResponse(TypedDict):
    status: Literal[0]
    msg: str
    data: List[HttpTraceDayCount]


def _parse_errors_only(raw: str | None) -> bool:
    if raw is None or raw == "":
        return False
    return raw.strip().lower() in _TRUE_TOKENS


def _clamp_page(page: int) -> int:
    return 1 if page < 1 else page


def _clamp_per_page(per_page: int) -> int:
    if per_page < 1:
        return 1
    if per_page > 100:
        return 100
    return per_page


def _empty_list_page(page: int, per_page: int) -> HttpTraceListPage:
    return {"rows": [], "count": 0, "page": page, "per_page": per_page}


def _invalid_path_prefix(raw: str) -> bool:
    if ".." in raw:
        return True
    if len(raw) > 256:
        return True
    if "\\" in raw or "\x00" in raw:
        return True
    return False


def _status_class_band(status_class: str) -> int:
    return int(status_class[0])


def _item_matches(
    item: HttpTraceListItem,
    method: str | None,
    path_prefix: str | None,
    status_class: str | None,
    user_id: str | None,
    errors_only: bool,
) -> bool:
    if method is not None and item["method"] != method:
        return False
    if path_prefix is not None and not item["path"].startswith(path_prefix):
        return False
    if status_class is not None:
        code = item["status_code"]
        if code is None:
            return False
        if code // 100 != _status_class_band(status_class):
            return False
    if user_id is not None and item["user_id"] != user_id:
        return False
    if errors_only:
        code = item["status_code"]
        if item["error_count"] <= 0 and (code is None or code < 400):
            return False
    return True


def merge_http_trace_list(
    date: str,
    page: int,
    per_page: int,
    method: str | None,
    path_prefix: str | None,
    status_class: str | None,
    user_id: str | None,
    errors_only: bool,
) -> HttpTraceListPage:
    merged: dict[str, HttpTraceListItem] = {}
    for record in list_http_traces_from_jsonl(date):
        merged[record["trace_id"]] = record
    for tid, item in http_trace_collector.get_active_traces().items():
        merged[tid] = item
    filtered = [
        item for item in merged.values() if _item_matches(item, method, path_prefix, status_class, user_id, errors_only)
    ]
    filtered.sort(key=lambda x: x["start_time"], reverse=True)
    total = len(filtered)
    per_page = _clamp_per_page(per_page)
    page = _clamp_page(page)
    max_page = max(1, (total + per_page - 1) // per_page) if total else 1
    if page > max_page:
        page = max_page
    start = (page - 1) * per_page
    return {
        "rows": filtered[start : start + per_page],
        "count": total,
        "page": page,
        "per_page": per_page,
    }


@app.get("/api/http-traces", summary="获取 HTTP 请求追踪列表", tags=HTTP_TRACE)
async def get_http_traces(
    date: Optional[str] = None,
    page: int = 1,
    per_page: int = 100,
    method: Optional[str] = None,
    path_prefix: Optional[str] = None,
    status_class: Optional[str] = None,
    user_id: Optional[str] = None,
    errors_only: Optional[str] = None,
    _user: SessionRecord = Depends(require_auth),
) -> HttpTraceListResponse:
    page = _clamp_page(page)
    per_page = _clamp_per_page(per_page)
    try:
        day = parse_iso_date(date, default_today=True)
    except PathEscapeError:
        return {"status": 1, "msg": "非法日期", "data": _empty_list_page(page, per_page)}

    method_filter: str | None = None
    if method is not None and method.strip() != "":
        method_filter = method.strip().upper()
        if method_filter not in _METHODS:
            return {"status": 1, "msg": "非法 method", "data": _empty_list_page(page, per_page)}

    prefix_filter: str | None = None
    if path_prefix is not None and path_prefix != "":
        if _invalid_path_prefix(path_prefix):
            return {"status": 1, "msg": "非法 path_prefix", "data": _empty_list_page(page, per_page)}
        prefix_filter = path_prefix

    class_filter: str | None = None
    if status_class is not None and status_class.strip() != "":
        class_filter = status_class.strip().lower()
        if class_filter not in _STATUS_CLASSES:
            return {"status": 1, "msg": "非法 status_class", "data": _empty_list_page(page, per_page)}

    user_filter: str | None = None
    if user_id is not None and user_id != "":
        user_filter = user_id

    data = await asyncio.to_thread(
        merge_http_trace_list,
        day,
        page,
        per_page,
        method_filter,
        prefix_filter,
        class_filter,
        user_filter,
        _parse_errors_only(errors_only),
    )
    return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/http-traces/daily_counts", summary="获取每日 HTTP 追踪数", tags=HTTP_TRACE)
async def get_http_trace_daily_counts(
    days: int = 60,
    _user: SessionRecord = Depends(require_auth),
) -> HttpTraceCountsResponse:
    clamped = max(1, min(days, 366))
    data = await asyncio.to_thread(daily_http_trace_counts, clamped)
    return {"status": 0, "msg": "ok", "data": data}


def _detail_from_disk(trace_id: str, day: str) -> Optional[HttpTraceDetail]:
    meta = get_http_trace_from_jsonl(trace_id, day)
    if meta is None:
        return None
    logs = get_http_trace_logs_from_daily_log(trace_id, day)
    return jsonl_record_to_detail(meta, logs)


@app.get("/api/http-traces/{trace_id}", summary="获取 HTTP 追踪详情", tags=HTTP_TRACE)
async def get_http_trace_detail(
    trace_id: str,
    date: Optional[str] = Query(default=None),
    _user: SessionRecord = Depends(require_auth),
) -> HttpTraceDetailResponse:
    try:
        day = parse_iso_date(date, default_today=True)
    except PathEscapeError:
        return {"status": 1, "msg": "非法日期", "data": None}

    memory = http_trace_collector.memory_detail(trace_id)
    if memory is not None:
        return {"status": 0, "msg": "ok", "data": memory}

    detail = await asyncio.to_thread(_detail_from_disk, trace_id, day)
    if detail is None:
        return {"status": 404, "msg": "追踪不存在", "data": None}
    return {"status": 0, "msg": "ok", "data": detail}
