"""HTTP 追踪 REST 合并 / 过滤 / 非法参数。"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from gsuid_core.logger import HttpTraceCollector
from gsuid_core.models import HttpTraceContext
from gsuid_core.http_trace_archive import write_http_trace_meta
from gsuid_core.webconsole.http_trace_api import (
    _clamp_page,
    _clamp_per_page,
    _invalid_path_prefix,
    merge_http_trace_list,
)


def _ctx(
    *,
    method: str = "GET",
    path: str = "/api/x",
    user_id: str | None = None,
) -> HttpTraceContext:
    tid = str(uuid.uuid4())
    return HttpTraceContext(
        trace_id=tid,
        short_id=tid[:8],
        method=method,
        path=path,
        client_ip="127.0.0.1",
        user_id=user_id,
        user_name=None,
        start_time=time.perf_counter(),
        start_ts=time.time(),
        content_length=None,
        query_redacted="",
        client_request_id=None,
    )


@pytest.fixture
def api_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, HttpTraceCollector]:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("gsuid_core.logger.LOG_PATH", log_dir)
    import gsuid_core.logger as lg
    from gsuid_core.webconsole import http_trace_api as api_mod

    http = HttpTraceCollector()
    monkeypatch.setattr(lg, "_http_trace_collector_instance", http)
    monkeypatch.setattr(lg, "http_trace_collector", http)
    monkeypatch.setattr(api_mod, "http_trace_collector", http)
    return log_dir, http


def test_clamp_page_and_per_page() -> None:
    assert _clamp_page(0) == 1
    assert _clamp_page(3) == 3
    assert _clamp_per_page(0) == 1
    assert _clamp_per_page(100) == 100
    assert _clamp_per_page(500) == 100


def test_path_prefix_dotdot() -> None:
    assert _invalid_path_prefix("/api/../etc")
    assert not _invalid_path_prefix("/api/plugins")


def test_merge_running_overlays_jsonl(api_env: tuple[Path, HttpTraceCollector]) -> None:
    _log_dir, http = api_env
    ctx = _ctx(path="/api/alpha")
    write_http_trace_meta(ctx, status="completed", log_count=0, duration_ms=3, status_code=200)
    http.start_trace(ctx)
    today = time.strftime("%Y-%m-%d")
    page = merge_http_trace_list(today, 1, 100, None, None, None, None, False)
    found = [r for r in page["rows"] if r["trace_id"] == ctx.trace_id]
    assert len(found) == 1
    assert found[0]["status"] == "running"
    http.finalize_trace(ctx.trace_id, 200)


def test_filters_and_errors_only_before_limit(api_env: tuple[Path, HttpTraceCollector]) -> None:
    _log_dir, http = api_env
    old = time.time() - 10
    newer = time.time()
    err = _ctx(method="POST", path="/api/err", user_id="u1")
    err.start_ts = old
    ok = _ctx(method="GET", path="/api/ok", user_id="u2")
    ok.start_ts = newer
    write_http_trace_meta(err, status="completed", log_count=2, duration_ms=9, status_code=500, error_count=1)
    write_http_trace_meta(ok, status="completed", log_count=0, duration_ms=1, status_code=200, error_count=0)
    today = time.strftime("%Y-%m-%d")
    posts = merge_http_trace_list(today, 1, 100, "POST", None, None, None, False)
    assert all(r["method"] == "POST" for r in posts["rows"])
    pref = merge_http_trace_list(today, 1, 100, None, "/api/err", None, None, False)
    assert all(r["path"].startswith("/api/err") for r in pref["rows"])
    cls = merge_http_trace_list(today, 1, 100, None, None, "5xx", None, False)
    assert all(r["status_code"] is not None and r["status_code"] // 100 == 5 for r in cls["rows"])
    user = merge_http_trace_list(today, 1, 100, None, None, None, "u1", False)
    assert all(r["user_id"] == "u1" for r in user["rows"])
    # per_page=1 若先截断会丢掉更旧的 5xx；errors_only 必须先过滤
    only_err = merge_http_trace_list(today, 1, 1, None, None, None, None, True)
    assert only_err["count"] == 1
    assert len(only_err["rows"]) == 1
    assert only_err["rows"][0]["trace_id"] == err.trace_id
    paged = merge_http_trace_list(today, 2, 1, None, None, None, None, False)
    assert paged["count"] == 2
    assert paged["page"] == 2
    assert len(paged["rows"]) == 1
    assert paged["rows"][0]["trace_id"] == err.trace_id


def test_silent_completed_empty_logs(api_env: tuple[Path, HttpTraceCollector]) -> None:
    _log_dir, http = api_env
    ctx = _ctx()
    http.start_trace(ctx)
    detail = http.memory_detail(ctx.trace_id)
    assert detail is not None
    assert detail["logs"] == []
    assert detail["log_count"] == 0
    http.finalize_trace(ctx.trace_id, 200)
    from gsuid_core.http_trace_archive import jsonl_record_to_detail, get_http_trace_from_jsonl

    meta = get_http_trace_from_jsonl(ctx.trace_id)
    assert meta is not None
    packed = jsonl_record_to_detail(meta, [])
    assert packed["logs"] == []
    assert packed["log_count"] == 0
