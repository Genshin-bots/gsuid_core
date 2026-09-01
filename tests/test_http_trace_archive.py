"""HTTP trace 按日分片：index 给列表，shard 给详情；旧单日 jsonl 仍可读。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from gsuid_core.models import HttpTraceContext
from gsuid_core.http_trace_archive import (
    _shard_key,
    write_http_trace_meta,
    daily_http_trace_counts,
    flush_http_trace_writes,
    get_http_trace_from_jsonl,
    list_http_traces_from_jsonl,
    count_http_traces_from_jsonl,
)


def _ctx(path: str = "/api/x", preview: str | None = None) -> HttpTraceContext:
    tid = str(uuid.uuid4())
    ctx = HttpTraceContext(
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
    if preview is not None:
        ctx.response_preview = preview
        ctx.response_content_type = "application/json"
    return ctx


@pytest.fixture
def archive_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("gsuid_core.logger.LOG_PATH", log_dir)
    return log_dir


def test_shard_key_uses_uuid_hex_prefix() -> None:
    assert _shard_key("de506eca-d8b0-4cb8-ae47-d860f0ae0df7") == "de"
    assert _shard_key("AB12") == "ab"
    assert _shard_key("??") == "zz"


def test_write_splits_index_and_shard(archive_env: Path) -> None:
    preview = '{"status":0,"msg":"ok","data":{"prompt":"' + ("x" * 200) + '"}}'
    ctx = _ctx(path="/api/canvas-backend/jobs/abc", preview=preview)
    write_http_trace_meta(ctx, status="completed", log_count=0, duration_ms=12, status_code=200)
    today = time.strftime("%Y-%m-%d")
    day = archive_env / "http_traces" / today
    shard = day / f"{_shard_key(ctx.trace_id)}.jsonl"
    index = day / "index.jsonl"
    assert not (archive_env / "http_traces" / f"{today}.jsonl").exists()
    listed = list_http_traces_from_jsonl(today)
    assert any(r["trace_id"] == ctx.trace_id for r in listed)
    assert index.exists()
    assert shard.exists()
    index_raw = index.read_text(encoding="utf-8")
    assert "response_preview" not in index_raw
    assert preview not in index_raw
    detail = get_http_trace_from_jsonl(ctx.trace_id, today)
    assert detail is not None
    assert "response_preview" in detail
    assert detail["response_preview"] == preview
    assert count_http_traces_from_jsonl(today) == 1


def test_legacy_flat_jsonl_still_readable(archive_env: Path) -> None:
    today = time.strftime("%Y-%m-%d")
    legacy = archive_env / "http_traces"
    legacy.mkdir(parents=True, exist_ok=True)
    tid = str(uuid.uuid4())
    line = {
        "trace_id": tid,
        "method": "GET",
        "path": "/api/old",
        "query_redacted": "",
        "client_ip": "127.0.0.1",
        "user_id": None,
        "user_name": None,
        "client_request_id": None,
        "content_length": None,
        "start_time": time.time(),
        "status": "completed",
        "log_count": 0,
        "duration_ms": 4,
        "status_code": 200,
        "error_count": 0,
        "response_preview": "legacy-preview",
    }
    (legacy / f"{today}.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    rows = list_http_traces_from_jsonl(today)
    assert len(rows) == 1
    assert rows[0]["trace_id"] == tid
    detail = get_http_trace_from_jsonl(tid, today)
    assert detail is not None
    assert detail["response_preview"] == "legacy-preview"


def test_list_ignores_legacy_when_index_exists(archive_env: Path) -> None:
    ctx = _ctx(path="/api/new")
    write_http_trace_meta(ctx, status="completed", log_count=0, duration_ms=1, status_code=200)
    today = time.strftime("%Y-%m-%d")
    flush_http_trace_writes()
    leftover_tid = str(uuid.uuid4())
    traces_root = archive_env / "http_traces"
    traces_root.mkdir(parents=True, exist_ok=True)
    legacy = traces_root / f"{today}.jsonl"
    legacy.write_text(
        json.dumps(
            {
                "trace_id": leftover_tid,
                "method": "GET",
                "path": "/api/old",
                "query_redacted": "",
                "client_ip": "127.0.0.1",
                "user_id": None,
                "user_name": None,
                "start_time": time.time(),
                "status": "completed",
                "log_count": 0,
                "duration_ms": 1,
                "status_code": 200,
                "error_count": 0,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    rows = list_http_traces_from_jsonl(today)
    ids = [r["trace_id"] for r in rows]
    assert ctx.trace_id in ids
    assert leftover_tid not in ids
    assert count_http_traces_from_jsonl(today) == 1


def test_detail_skips_oversized_legacy(archive_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import gsuid_core.day_jsonl_store as store

    monkeypatch.setattr(store, "_MAX_FULL_SCAN_BYTES", 40)
    today = time.strftime("%Y-%m-%d")
    tid = str(uuid.uuid4())
    payload = {
        "trace_id": tid,
        "method": "GET",
        "path": "/api/old",
        "query_redacted": "",
        "client_ip": "127.0.0.1",
        "start_time": time.time(),
        "status": "completed",
        "log_count": 0,
        "pad": "x" * 80,
    }
    legacy = archive_env / "http_traces"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / f"{today}.jsonl").write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    assert get_http_trace_from_jsonl(tid, today) is None


def test_http_calendar_includes_running(archive_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gsuid_core.logger import HttpTraceCollector

    assert archive_env.exists()
    http = HttpTraceCollector()
    monkeypatch.setattr("gsuid_core.logger.http_trace_collector", http)
    ctx = _ctx()
    http.start_trace(ctx)
    counts = daily_http_trace_counts(1)
    assert len(counts) == 1
    assert counts[0]["count"] >= 1
    http.finalize_trace(ctx.trace_id, 200)
    counts2 = daily_http_trace_counts(1)
    assert counts2[0]["count"] == 1
