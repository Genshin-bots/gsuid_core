"""命令 trace 复用 day_jsonl_store：分片 + 旧整日 jsonl 可读。"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

import pytest

from gsuid_core.models import TraceContext
from gsuid_core.trace_archive import (
    write_trace_meta,
    get_trace_from_jsonl,
    list_traces_from_jsonl,
    count_traces_from_jsonl,
)
from gsuid_core.day_jsonl_store import shard_key


def _ctx() -> TraceContext:
    tid = str(uuid.uuid4())
    return TraceContext(
        trace_id=tid,
        short_id=tid[:8],
        command="签到",
        user_id="u1",
        group_id="g1",
        bot_id="b",
        session_id="s",
        start_time=time.perf_counter(),
        start_ts=time.time(),
    )


@pytest.fixture
def cmd_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    traces = log_dir / "traces"
    monkeypatch.setattr("gsuid_core.logger.LOG_PATH", log_dir)
    monkeypatch.setattr("gsuid_core.trace_archive.LOG_PATH", log_dir)
    monkeypatch.setattr("gsuid_core.trace_archive.TRACE_JSONL_PATH", traces)
    return log_dir


def test_command_write_uses_day_dir(cmd_env: Path) -> None:
    ctx = _ctx()
    write_trace_meta(ctx.trace_id, ctx, status="running", log_count=0)
    write_trace_meta(ctx.trace_id, ctx, status="completed", log_count=3, duration_ms=40)
    today = time.strftime("%Y-%m-%d")
    rows = list_traces_from_jsonl(today)
    day = cmd_env / "traces" / today
    assert (day / "index.jsonl").exists()
    assert (day / f"{shard_key(ctx.trace_id)}.jsonl").exists()
    assert not (cmd_env / "traces" / f"{today}.jsonl").exists()
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["duration_ms"] == 40
    detail = get_trace_from_jsonl(ctx.trace_id, today)
    assert detail is not None
    assert detail["bot_id"] == "b"
    assert count_traces_from_jsonl(today) == 1


def test_http_and_command_share_one_writer_thread(cmd_env: Path) -> None:
    import threading

    from gsuid_core.models import HttpTraceContext
    from gsuid_core.http_trace_archive import write_http_trace_meta

    before = {t.name for t in threading.enumerate()}
    ctx = _ctx()
    write_trace_meta(ctx.trace_id, ctx, status="completed", log_count=1, duration_ms=1)
    http = HttpTraceContext(
        trace_id=str(uuid.uuid4()),
        short_id="abcd1234",
        method="GET",
        path="/api/x",
        client_ip="127.0.0.1",
        user_id=None,
        user_name=None,
        start_time=time.perf_counter(),
        start_ts=time.time(),
        content_length=None,
        query_redacted="",
        client_request_id=None,
    )
    write_http_trace_meta(http, status="completed", log_count=0, duration_ms=2, status_code=200)
    names = [t.name for t in threading.enumerate() if t.name == "trace-jsonl"]
    assert len(names) == 1
    assert "trace-jsonl" not in before or names[0] == "trace-jsonl"


def test_command_legacy_flat_jsonl_readable(cmd_env: Path) -> None:
    today = time.strftime("%Y-%m-%d")
    traces = cmd_env / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    tid = str(uuid.uuid4())
    line = {
        "trace_id": tid,
        "command": "旧令",
        "user_id": "u2",
        "group_id": None,
        "bot_id": "b",
        "session_id": "s",
        "start_time": time.time(),
        "status": "completed",
        "log_count": 1,
        "duration_ms": 8,
    }
    (traces / f"{today}.jsonl").write_text(json.dumps(line, ensure_ascii=False) + "\n", encoding="utf-8")
    rows = list_traces_from_jsonl(today)
    assert len(rows) == 1
    assert rows[0]["command"] == "旧令"
    detail = get_trace_from_jsonl(tid, today)
    assert detail is not None
    assert detail["session_id"] == "s"
