"""HttpTraceMiddleware：不读 body、排除表、SSE detach、499、响应头 copy。"""

from __future__ import annotations

import uuid
import asyncio
from typing import List
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from starlette.types import Send, Scope, Message, Receive
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from gsuid_core.logger import HttpTraceCollector
from gsuid_core.http_trace_archive import get_http_trace_from_jsonl, list_http_traces_from_jsonl
from gsuid_core.http_trace_middleware import (
    HttpTraceMiddleware,
    norm_http_path,
    is_api_http_path,
    redact_query_string,
    is_http_trace_excluded,
)


@pytest.fixture
def mw_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, HttpTraceCollector]:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("gsuid_core.logger.LOG_PATH", log_dir)
    import gsuid_core.logger as lg

    http = HttpTraceCollector()
    monkeypatch.setattr(lg, "_http_trace_collector_instance", http)
    monkeypatch.setattr(lg, "http_trace_collector", http)
    monkeypatch.setattr(
        "gsuid_core.http_trace_middleware._runtime_mcp_path",
        lambda: "/api/mcp",
    )
    return log_dir, http


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(HttpTraceMiddleware)

    @app.post("/api/echo")
    async def echo(request: Request) -> dict:
        return await request.json()

    @app.get("/api/ping")
    async def ping() -> dict:
        from gsuid_core.logger import logger

        logger.info("ping-inside")
        return {"ok": True}

    @app.get("/api/q")
    async def query_echo(x: str = "") -> dict:
        return {"x": x, "api_key": "sk-secret-value"}

    @app.get("/api/system/health")
    async def health() -> dict:
        return {"ok": True}

    @app.get("/api/logs/stream")
    async def log_stream() -> StreamingResponse:
        async def gen():
            yield b"data: x\n\n"
            await asyncio.sleep(0.05)
            yield b"data: y\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/custom-sse")
    async def custom_sse() -> StreamingResponse:
        async def gen():
            yield b"data: a\n\n"
            await asyncio.sleep(0.05)
            yield b"data: b\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/v1/agent/chat/stream")
    async def agent_stream() -> StreamingResponse:
        async def gen():
            yield b"data: z\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def test_norm_and_api_path() -> None:
    assert norm_http_path("/api/x/") == "/api/x"
    assert is_api_http_path("/api")
    assert is_api_http_path("/api/x")
    assert not is_api_http_path("/app")
    assert not is_api_http_path("/ws/bot")


def test_exclude_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "gsuid_core.http_trace_middleware._runtime_mcp_path",
        lambda: "/api/mcp",
    )
    assert is_http_trace_excluded("/api/system/health")
    assert is_http_trace_excluded("/api/traces")
    assert is_http_trace_excluded("/api/traces/abc")
    assert is_http_trace_excluded("/api/http-traces")
    assert is_http_trace_excluded("/api/http-traces/abc")
    assert is_http_trace_excluded("/api/logs")
    assert is_http_trace_excluded("/api/logs/stream")
    assert is_http_trace_excluded("/api/logs/config")
    assert is_http_trace_excluded("/api/dashboard")
    assert is_http_trace_excluded("/api/dashboard/metrics")
    assert is_http_trace_excluded("/api/version")
    assert is_http_trace_excluded("/api/version/bots")
    assert is_http_trace_excluded("/api/v1/agent/chat/stream")
    assert is_http_trace_excluded("/api/mcp")
    assert is_http_trace_excluded("/api/mcp/foo")
    assert not is_http_trace_excluded("/api/ai/mcp")
    assert is_http_trace_excluded("/api/v1/agent/health")
    assert not is_http_trace_excluded("/api/auth/login", "POST")
    assert not is_http_trace_excluded("/api/system/restart", "POST")
    assert is_http_trace_excluded("/api/auth/me", "GET")
    assert is_http_trace_excluded("/api/auth/pubkey", "GET")
    assert is_http_trace_excluded("/api/auth/admin/exists", "GET")
    assert is_http_trace_excluded("/api/auth/avatar/a.png", "GET")
    assert is_http_trace_excluded("/api/brand", "GET")
    assert is_http_trace_excluded("/api/brand/icon", "GET")
    assert is_http_trace_excluded("/api/theme/config", "GET")
    assert is_http_trace_excluded("/api/theme/presets", "GET")
    assert is_http_trace_excluded("/api/plugins/icon/Foo", "GET")
    assert is_http_trace_excluded("/api/assets/preview", "GET")
    assert is_http_trace_excluded("/api/system/info", "GET")
    assert is_http_trace_excluded("/api/plugins/list", "GET")
    assert is_http_trace_excluded("/api/plugin-pages", "GET")
    assert is_http_trace_excluded("/api/persona/list", "GET")
    assert is_http_trace_excluded("/api/persona/foo/avatar", "GET")
    assert is_http_trace_excluded("/api/persona/foo/image", "GET")
    assert is_http_trace_excluded("/api/getImage/png/a/b", "GET")
    assert is_http_trace_excluded("/api/image/abc", "GET")
    assert is_http_trace_excluded("/api/meme/image/abc", "GET")
    assert is_http_trace_excluded("/api/ops/bots", "GET")
    assert is_http_trace_excluded("/api/ai/statistics/summary", "GET")
    assert is_http_trace_excluded("/api/scheduler/jobs", "GET")
    assert is_http_trace_excluded("/api/live-chat/state", "GET")
    assert not is_http_trace_excluded("/api/auth/login", "POST")
    assert not is_http_trace_excluded("/api/auth/password", "POST")
    assert not is_http_trace_excluded("/api/auth/avatar", "POST")
    assert not is_http_trace_excluded("/api/brand", "POST")
    assert not is_http_trace_excluded("/api/brand/icon", "POST")
    assert not is_http_trace_excluded("/api/theme/config", "POST")
    assert not is_http_trace_excluded("/api/theme/presets/save", "POST")
    assert not is_http_trace_excluded("/api/persona/foo/config", "GET")
    assert not is_http_trace_excluded("/api/plugins/FooPlugin", "GET")
    assert not is_http_trace_excluded("/api/ai/images/list", "GET")
    assert not is_http_trace_excluded("/api/ops/intent", "POST")
    assert not is_http_trace_excluded("/api/scheduler/jobs/x/run", "POST")
    assert not is_http_trace_excluded("/api/v1/agent/sessions/reset", "POST")
    assert not is_http_trace_excluded("/api/canvas-backend/jobs/3171cdbc120fe54f6154f6f7f78423ea", "GET")
    assert not is_http_trace_excluded("/api/canvas-backend/collab/85b081043fb49ade/presence", "GET")
    assert not is_http_trace_excluded("/api/RH_ComfyUI/models/estimate", "GET")


def test_redact_query() -> None:
    assert redact_query_string(b"") == ""
    out = redact_query_string(b"a=1&token=secret&api_key=k")
    assert "a=1" in out
    assert "token=****" in out
    assert "api_key=****" in out
    assert "secret" not in out
    long_val = "x" * 600
    cut = redact_query_string(f"q={long_val}".encode("ascii"))
    assert len(cut) <= 512


def test_query_and_masked_json_preview(mw_env: tuple[Path, HttpTraceCollector]) -> None:
    client = TestClient(_app())
    r = client.get("/api/q?x=hi&token=abc123")
    assert r.status_code == 200
    tid = r.headers["x-http-trace-id"]
    meta = get_http_trace_from_jsonl(tid)
    assert meta is not None
    assert "token=****" in meta["query_redacted"]
    assert "x=hi" in meta["query_redacted"]
    preview = meta["response_preview"] if "response_preview" in meta else None
    assert preview is not None
    assert "hi" in preview
    assert "sk-secret-value" not in preview
    assert "****" in preview


def test_post_json_body_identity(mw_env: tuple[Path, HttpTraceCollector]) -> None:
    client = TestClient(_app())
    payload = {"hello": "world", "n": 3}
    r = client.post("/api/echo", json=payload)
    assert r.status_code == 200
    assert r.json() == payload
    assert "x-http-trace-id" in r.headers
    tid = r.headers["x-http-trace-id"]
    uuid.UUID(tid)
    meta = get_http_trace_from_jsonl(tid)
    assert meta is not None
    assert meta["status"] == "completed"
    assert meta["status_code"] == 200
    assert meta["method"] == "POST"
    assert meta["path"] == "/api/echo"


def test_health_not_traced(mw_env: tuple[Path, HttpTraceCollector]) -> None:
    _log_dir, http = mw_env
    client = TestClient(_app())
    r = client.get("/api/system/health")
    assert r.status_code == 200
    assert "x-http-trace-id" not in r.headers
    assert http.get_active_traces() == {}
    assert list_http_traces_from_jsonl() == []


def test_logs_stream_excluded(mw_env: tuple[Path, HttpTraceCollector]) -> None:
    _log_dir, http = mw_env
    client = TestClient(_app())
    r = client.get("/api/logs/stream")
    assert r.status_code == 200
    assert http.get_active_traces() == {}
    assert list_http_traces_from_jsonl() == []


def test_agent_stream_excluded(mw_env: tuple[Path, HttpTraceCollector]) -> None:
    _log_dir, http = mw_env
    client = TestClient(_app())
    r = client.get("/api/v1/agent/chat/stream")
    assert r.status_code == 200
    assert list_http_traces_from_jsonl() == []


def test_sse_content_type_detaches(mw_env: tuple[Path, HttpTraceCollector]) -> None:
    _log_dir, http = mw_env
    client = TestClient(_app())
    r = client.get("/api/custom-sse")
    assert r.status_code == 200
    assert http.get_active_traces() == {}
    rows = list_http_traces_from_jsonl()
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["path"] == "/api/custom-sse"


def test_start_headers_are_byte_tuples(mw_env: tuple[Path, HttpTraceCollector]) -> None:
    captured: List[Message] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def send_wrap(message: Message) -> None:
        captured.append(message)

    async def receive_wrap() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def run() -> None:
        mw = HttpTraceMiddleware(inner)
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/hdr",
            "raw_path": b"/api/hdr",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
        }
        await mw(scope, receive_wrap, send_wrap)

    asyncio.run(run())
    starts = [m for m in captured if m["type"] == "http.response.start"]
    assert len(starts) == 1
    headers = starts[0]["headers"]
    assert isinstance(headers, list)
    for name, value in headers:
        assert isinstance(name, (bytes, bytearray))
        assert isinstance(value, (bytes, bytearray))
    assert any(name == b"x-http-trace-id" for name, _value in headers)


def test_disconnect_cancelled_is_499(mw_env: tuple[Path, HttpTraceCollector]) -> None:
    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise asyncio.CancelledError()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive_wrap() -> Message:
        return {"type": "http.disconnect"}

    async def send_wrap(message: Message) -> None:
        return None

    async def run() -> None:
        mw = HttpTraceMiddleware(inner)
        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/abort",
            "raw_path": b"/api/abort",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 80),
        }
        with pytest.raises(asyncio.CancelledError):
            await mw(scope, receive_wrap, send_wrap)

    asyncio.run(run())
    rows = list_http_traces_from_jsonl()
    assert len(rows) == 1
    assert rows[0]["status_code"] == 499
    assert rows[0]["status"] == "completed"


def test_ping_collects_logger_line(mw_env: tuple[Path, HttpTraceCollector]) -> None:
    client = TestClient(_app())
    r = client.get("/api/ping")
    assert r.status_code == 200
    tid = r.headers["x-http-trace-id"]
    meta = get_http_trace_from_jsonl(tid)
    assert meta is not None
    assert meta["log_count"] >= 1
