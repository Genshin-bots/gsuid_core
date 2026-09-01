"""HTTP `/api` 请求追踪 REST：列表 / 日历 / 详情。自身路径不进入追踪。"""

from __future__ import annotations

import asyncio
from typing import List, Literal, TypeVar, Callable, Optional, TypedDict
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from fastapi import Query, Depends

from gsuid_core.logger import http_trace_collector
from gsuid_core.models import HttpTraceDetail, HttpTraceLogLine, HttpTraceListItem
from gsuid_core.utils.path_safety import PathEscapeError, parse_iso_date
from gsuid_core.http_trace_archive import (
    HttpTraceDayCount,
    iter_http_list_items,
    jsonl_record_to_detail,
    daily_http_trace_counts,
    flush_http_trace_writes,
    get_http_trace_from_jsonl,
    count_http_traces_from_jsonl,
    get_http_trace_logs_from_daily_log,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.webconsole.session_store import SessionRecord

from ._api_tags import HTTP_TRACE

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"})
_STATUS_CLASSES = frozenset({"2xx", "3xx", "4xx", "5xx"})
_TRUE_TOKENS = frozenset({"1", "true", "yes"})

# json.loads 占 GIL：独立 1 worker，同参合并，避免轮询叠成全表扫描
_READ_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="http-trace-read")
_T = TypeVar("_T")
_LIST_INFLIGHT: dict[tuple[str, ...], asyncio.Future[HttpTraceListPage]] = {}
_COUNT_INFLIGHT: dict[tuple[str, ...], asyncio.Future[List[HttpTraceDayCount]]] = {}
_DETAIL_INFLIGHT: dict[tuple[str, ...], asyncio.Future[Optional[HttpTraceDetail]]] = {}
_INFLIGHT_LOCK: asyncio.Lock | None = None


def _inflight_lock() -> asyncio.Lock:
    global _INFLIGHT_LOCK
    if _INFLIGHT_LOCK is None:
        _INFLIGHT_LOCK = asyncio.Lock()
    return _INFLIGHT_LOCK


def _shutdown_http_trace_reads() -> None:
    _READ_EXECUTOR.shutdown(wait=False, cancel_futures=True)


try:
    from gsuid_core.server import on_core_shutdown

    on_core_shutdown(_shutdown_http_trace_reads)
except ImportError:
    pass


async def _coalesced_call(
    table: dict[tuple[str, ...], asyncio.Future[_T]],
    key: tuple[str, ...],
    thunk: Callable[[], _T],
) -> _T:
    loop = asyncio.get_running_loop()
    lock = _inflight_lock()
    async with lock:
        if key in table:
            fut = table[key]
        else:
            fut = loop.run_in_executor(_READ_EXECUTOR, thunk)
            table[key] = fut
    try:
        return await fut
    finally:
        async with lock:
            if key in table and table[key] is fut:
                del table[key]


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
    running: dict[str, HttpTraceListItem] | None = None,
) -> HttpTraceListPage:
    flush_http_trace_writes()
    per_page = _clamp_per_page(per_page)
    page = _clamp_page(page)
    running_map = running if running is not None else http_trace_collector.get_active_traces()
    running_items = sorted(running_map.values(), key=lambda x: x["start_time"], reverse=True)
    unfiltered = method is None and path_prefix is None and status_class is None and user_id is None and not errors_only
    if unfiltered:
        disk_n = count_http_traces_from_jsonl(date, flush=False)
        total = disk_n + len(running_items)
        max_page = max(1, (total + per_page - 1) // per_page) if total else 1
        if page > max_page:
            page = max_page
        skip = (page - 1) * per_page
        kept: list[HttpTraceListItem] = []
        seen: set[str] = set()
        taken = 0

        def consider(item: HttpTraceListItem) -> bool:
            nonlocal taken
            tid = item["trace_id"]
            if tid in seen:
                return False
            seen.add(tid)
            if taken < skip:
                taken += 1
                return False
            if len(kept) < per_page:
                kept.append(item)
            return len(kept) >= per_page

        filled = False
        for item in running_items:
            if consider(item):
                filled = True
                break
        if not filled:
            for item in iter_http_list_items(date, flush=False):
                if consider(item):
                    break
        kept.sort(key=lambda x: x["start_time"], reverse=True)
        return {"rows": kept, "count": total, "page": page, "per_page": per_page}

    skip = (page - 1) * per_page
    kept_f: list[HttpTraceListItem] = []
    last_page: deque[HttpTraceListItem] = deque(maxlen=per_page)
    seen_f: set[str] = set()
    total_f = 0

    def consider_f(item: HttpTraceListItem) -> None:
        nonlocal total_f
        tid = item["trace_id"]
        if tid in seen_f:
            return
        seen_f.add(tid)
        if not _item_matches(item, method, path_prefix, status_class, user_id, errors_only):
            return
        last_page.append(item)
        if skip <= total_f < skip + per_page:
            kept_f.append(item)
        total_f += 1

    for item in running_items:
        consider_f(item)
    for item in iter_http_list_items(date, flush=False):
        consider_f(item)
    max_page_f = max(1, (total_f + per_page - 1) // per_page) if total_f else 1
    if page > max_page_f:
        page = max_page_f
        kept_f = list(last_page)
    kept_f.sort(key=lambda x: x["start_time"], reverse=True)
    return {"rows": kept_f, "count": total_f, "page": page, "per_page": per_page}


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

    errors = _parse_errors_only(errors_only)
    running = http_trace_collector.get_active_traces()
    key = (
        day,
        str(page),
        str(per_page),
        method_filter if method_filter is not None else "",
        prefix_filter if prefix_filter is not None else "",
        class_filter if class_filter is not None else "",
        user_filter if user_filter is not None else "",
        "1" if errors else "0",
    )

    def _thunk() -> HttpTraceListPage:
        return merge_http_trace_list(
            day,
            page,
            per_page,
            method_filter,
            prefix_filter,
            class_filter,
            user_filter,
            errors,
            running,
        )

    data = await _coalesced_call(_LIST_INFLIGHT, key, _thunk)
    return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/http-traces/daily_counts", summary="获取每日 HTTP 追踪数", tags=HTTP_TRACE)
async def get_http_trace_daily_counts(
    days: int = 60,
    _user: SessionRecord = Depends(require_auth),
) -> HttpTraceCountsResponse:
    clamped = max(1, min(days, 366))
    key = (str(clamped),)

    def _thunk() -> List[HttpTraceDayCount]:
        return daily_http_trace_counts(clamped)

    data = await _coalesced_call(_COUNT_INFLIGHT, key, _thunk)
    return {"status": 0, "msg": "ok", "data": data}


def _detail_from_disk(trace_id: str, day: str) -> Optional[HttpTraceDetail]:
    meta = get_http_trace_from_jsonl(trace_id, day)
    if meta is None:
        return None
    logs: List[HttpTraceLogLine]
    if meta["log_count"] <= 0:
        logs = []
    else:
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

    key = (trace_id, day)

    def _thunk() -> Optional[HttpTraceDetail]:
        return _detail_from_disk(trace_id, day)

    detail = await _coalesced_call(_DETAIL_INFLIGHT, key, _thunk)
    if detail is None:
        return {"status": 404, "msg": "追踪不存在", "data": None}
    return {"status": 0, "msg": "ok", "data": detail}
