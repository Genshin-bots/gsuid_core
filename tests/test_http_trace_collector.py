"""HttpTraceCollector：键 http_trace_id、不污染命令 traces、无 info Start/End。"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from gsuid_core.logger import (
    TraceCollector,
    HttpTraceCollector,
    logger,
    log_history,
    bind_trace_context,
    clear_trace_context,
    bind_http_trace_context,
    clear_http_trace_context,
)
from gsuid_core.models import TraceContext, HttpTraceContext


def _http_ctx(path: str = "/api/x") -> HttpTraceContext:
    tid = str(uuid.uuid4())
    return HttpTraceContext(
        trace_id=tid,
        short_id=tid[:8],
        method="GET",
        path=path,
        client_ip="127.0.0.1",
        user_id=None,
        user_name=None,
        start_time=time.perf_counter(),
        start_ts=time.time(),
        content_length=None,
        query_redacted="",
        client_request_id=None,
    )


def _cmd_ctx() -> TraceContext:
    tid = str(uuid.uuid4())
    return TraceContext(
        trace_id=tid,
        short_id=tid[:8],
        command="ping",
        user_id="u1",
        group_id=None,
        bot_id="b",
        session_id="s",
        start_time=time.perf_counter(),
        start_ts=time.time(),
    )


@pytest.fixture
def http_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, HttpTraceCollector, TraceCollector]:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("gsuid_core.logger.LOG_PATH", log_dir)
    monkeypatch.setattr("gsuid_core.trace_archive.LOG_PATH", log_dir)
    monkeypatch.setattr("gsuid_core.trace_archive.TRACE_JSONL_PATH", log_dir / "traces")
    import gsuid_core.logger as lg

    http = HttpTraceCollector()
    cmd = TraceCollector()
    monkeypatch.setattr(lg, "_http_trace_collector_instance", http)
    monkeypatch.setattr(lg, "http_trace_collector", http)
    monkeypatch.setattr(lg, "_trace_collector_instance", cmd)
    monkeypatch.setattr(lg, "trace_collector", cmd)
    return log_dir, http, cmd


def test_http_only_bind_goes_to_http_bucket(
    http_logs: tuple[Path, HttpTraceCollector, TraceCollector],
) -> None:
    _log_dir, http, cmd = http_logs
    ctx = _http_ctx()
    bind_http_trace_context(ctx)
    http.start_trace(ctx)
    try:
        logger.info("http-only-line")
        logs = http.get_trace_logs(ctx.trace_id)
        assert logs is not None
        assert any("http-only-line" in e.event for e in logs)
        assert cmd.get_trace_logs(ctx.trace_id) is None
    finally:
        clear_http_trace_context()
        http.finalize_trace(ctx.trace_id, 200)


def test_dual_bind_uses_own_keys(
    http_logs: tuple[Path, HttpTraceCollector, TraceCollector],
) -> None:
    _log_dir, http, cmd = http_logs
    hctx = _http_ctx()
    cctx = _cmd_ctx()
    cmd.start_trace(cctx)
    bind_trace_context(cctx)
    bind_http_trace_context(hctx)
    http.start_trace(hctx)
    try:
        logger.info("both-keys")
        h_logs = http.get_trace_logs(hctx.trace_id)
        c_logs = cmd.get_trace_logs(cctx.trace_id)
        assert h_logs is not None and any("both-keys" in e.event for e in h_logs)
        assert c_logs is not None and any("both-keys" in e.event for e in c_logs)
        assert http.get_trace_logs(cctx.trace_id) is None
        assert cmd.get_trace_logs(hctx.trace_id) is None
    finally:
        clear_http_trace_context()
        clear_trace_context()
        http.finalize_trace(hctx.trace_id, 200)
        cmd.finalize_trace(cctx.trace_id)


def test_command_jsonl_dir_untouched(
    http_logs: tuple[Path, HttpTraceCollector, TraceCollector],
) -> None:
    log_dir, http, _cmd = http_logs
    ctx = _http_ctx()
    bind_http_trace_context(ctx)
    http.start_trace(ctx)
    http.finalize_trace(ctx.trace_id, 200)
    clear_http_trace_context()
    from gsuid_core.http_trace_archive import flush_http_trace_writes

    flush_http_trace_writes()
    traces_dir = log_dir / "traces"
    assert not traces_dir.exists() or not any(traces_dir.iterdir())
    http_dir = log_dir / "http_traces"
    assert http_dir.exists()
    assert any(http_dir.rglob("*.jsonl"))


def test_command_collector_capacity_unchanged(
    http_logs: tuple[Path, HttpTraceCollector, TraceCollector],
) -> None:
    _log_dir, http, cmd = http_logs
    before = len(cmd.get_active_traces())
    ctx = _http_ctx()
    http.start_trace(ctx)
    assert len(cmd.get_active_traces()) == before
    http.finalize_trace(ctx.trace_id, 204)


def test_no_info_start_end_in_sse_buffer(
    http_logs: tuple[Path, HttpTraceCollector, TraceCollector],
) -> None:
    _log_dir, http, _cmd = http_logs
    before = [r.gevent for r in log_history]
    ctx = _http_ctx()
    bind_http_trace_context(ctx)
    http.start_trace(ctx)
    http.finalize_trace(ctx.trace_id, 200)
    clear_http_trace_context()
    added = [r.gevent for r in log_history if r.gevent not in before]
    assert not any("HttpTraceStart" in g or "HttpTraceEnd" in g for g in added)
    assert not any("[TraceStart]" in g and "/api/x" in g for g in added)


def test_silent_request_empty_logs_no_breadcrumb(
    http_logs: tuple[Path, HttpTraceCollector, TraceCollector],
) -> None:
    log_dir, http, _cmd = http_logs
    ctx = _http_ctx()
    bind_http_trace_context(ctx)
    http.start_trace(ctx)
    assert http.get_trace_logs(ctx.trace_id) == []
    http.finalize_trace(ctx.trace_id, 200)
    clear_http_trace_context()
    from gsuid_core.http_trace_archive import get_http_trace_from_jsonl

    meta = get_http_trace_from_jsonl(ctx.trace_id)
    assert meta is not None
    assert meta["log_count"] == 0


def test_running_http_trace_not_written_until_finalize(
    http_logs: tuple[Path, HttpTraceCollector, TraceCollector],
) -> None:
    log_dir, http, _cmd = http_logs
    ctx = _http_ctx()
    http.start_trace(ctx)
    from gsuid_core.http_trace_archive import list_http_traces_from_jsonl

    rows = list_http_traces_from_jsonl()
    assert not any(r["trace_id"] == ctx.trace_id for r in rows)
    http.finalize_trace(ctx.trace_id, 201)
    rows2 = list_http_traces_from_jsonl()
    found = [r for r in rows2 if r["trace_id"] == ctx.trace_id]
    assert len(found) == 1
    assert found[0]["status"] == "completed"
    assert found[0]["status_code"] == 201
