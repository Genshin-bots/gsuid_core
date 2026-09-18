"""McpHttpGuard：MCP HTTP 子应用护栏（隔离上层取消 + 无响应自愈）。

回归的是线上事故：stateless Streamable HTTP 的终身 task group 被子任务取消毒化后，
子应用不写响应就返回 → 上层 middleware 抛 "No response returned." → 永久 500。
"""

from __future__ import annotations

import json
import asyncio
import contextlib
from typing import List
from collections.abc import AsyncIterator

import anyio
import pytest
from starlette.types import Send, Scope, ASGIApp, Message, Receive
from starlette.routing import Mount

from gsuid_core.ai_core.mcp import server as mcp_server


def _scope() -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/mcp/",
        "raw_path": b"/api/mcp/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }


async def _receive() -> Message:
    return {"type": "http.request", "body": b"{}", "more_body": False}


def _collector() -> tuple[List[Message], Send]:
    sent: List[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    return sent, send


def _stub_rebuild(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    calls: List[int] = []

    async def fake() -> None:
        calls.append(1)

    monkeypatch.setattr(mcp_server, "_rebuild_http_mcp_session", fake)
    return calls


def _start_messages(sent: List[Message]) -> List[Message]:
    return [message for message in sent if message["type"] == "http.response.start"]


def test_guard_forwards_normal_response(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_rebuild(monkeypatch)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run() -> List[Message]:
        sent, send = _collector()
        await mcp_server.McpHttpGuard(inner)(_scope(), _receive, send)
        return sent

    sent = asyncio.run(run())
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    assert calls == []


def test_guard_replies_503_and_rebuilds_when_app_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_rebuild(monkeypatch)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        return None

    async def run() -> List[Message]:
        sent, send = _collector()
        await mcp_server.McpHttpGuard(inner)(_scope(), _receive, send)
        return sent

    sent = asyncio.run(run())
    starts = _start_messages(sent)
    assert len(starts) == 1
    assert starts[0]["status"] == 503
    bodies = [message["body"] for message in sent if message["type"] == "http.response.body"]
    assert len(bodies) == 1
    raw = bodies[0]
    assert isinstance(raw, bytes)
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    error = payload["error"]
    assert isinstance(error, dict)
    assert error["code"] == -32603
    assert calls == [1]


def test_guard_shields_inner_app_from_outer_cancellation() -> None:
    finished = False

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def run() -> None:
        sent, send = _collector()
        with anyio.move_on_after(0.01):
            await mcp_server.McpHttpGuard(inner)(_scope(), _receive, send)
        assert _start_messages(sent)

    asyncio.run(run())
    assert finished is True


class _FakeHttpApp:
    """fastmcp http_app() 的替身：记录 lifespan 进出次数。"""

    def __init__(self, fail_enter: bool = False) -> None:
        self.entered = 0
        self.exited = 0
        self.fail_enter = fail_enter

    @contextlib.asynccontextmanager
    async def _cm(self) -> AsyncIterator[None]:
        if self.fail_enter:
            raise RuntimeError("lifespan boom")
        self.entered += 1
        try:
            yield
        finally:
            self.exited += 1

    def lifespan(self, app: _FakeHttpApp) -> contextlib.AbstractAsyncContextManager[None]:
        return self._cm()


class _FakeMcpServer:
    def __init__(self) -> None:
        self.apps: List[_FakeHttpApp] = []
        self.fail = False
        self.fail_enter = False

    def http_app(self, path: str, transport: str, stateless_http: bool) -> _FakeHttpApp:
        if self.fail:
            raise RuntimeError("http_app boom")
        app = _FakeHttpApp(fail_enter=self.fail_enter)
        self.apps.append(app)
        return app


class _FakeRouter:
    def __init__(self) -> None:
        self.routes: List[Mount] = []


class _FakeMainApp:
    def __init__(self) -> None:
        self.router = _FakeRouter()

    def mount(self, path: str, app: ASGIApp) -> None:
        self.router.routes.append(Mount(path, app))


def _patch_rebuild_env(
    monkeypatch: pytest.MonkeyPatch,
    server: _FakeMcpServer,
    main_app: _FakeMainApp,
) -> None:
    monkeypatch.setattr(mcp_server, "_mcp_server", server)
    monkeypatch.setattr(mcp_server, "_mcp_mount_path", "/api/mcp")
    monkeypatch.setattr(mcp_server, "_http_mounted", True)
    monkeypatch.setattr(mcp_server, "_mcp_last_rebuild", 0.0)
    monkeypatch.setattr("gsuid_core.app_life.app", main_app)


def test_rebuild_swaps_mount_and_resets_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_server = _FakeMcpServer()
    fake_main = _FakeMainApp()
    old_app = _FakeHttpApp()
    _patch_rebuild_env(monkeypatch, fake_server, fake_main)

    async def run() -> None:
        old_cm = old_app.lifespan(old_app)
        await old_cm.__aenter__()
        monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", old_cm)
        await mcp_server._rebuild_http_mcp_session()
        # 冷却内的第二次调用不应再建
        await mcp_server._rebuild_http_mcp_session()

    asyncio.run(run())

    assert len(fake_server.apps) == 1
    assert fake_server.apps[0].entered == 1
    mounts = [route for route in fake_main.router.routes if isinstance(route, Mount)]
    assert len(mounts) == 1
    assert mounts[0].path == "/api/mcp"
    assert isinstance(mounts[0].app, mcp_server.McpHttpGuard)
    assert old_app.exited == 1
    assert mcp_server._mcp_lifespan_cm is not None


def test_rebuild_failure_keeps_old_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_server = _FakeMcpServer()
    fake_server.fail = True
    fake_main = _FakeMainApp()
    old_app = _FakeHttpApp()
    _patch_rebuild_env(monkeypatch, fake_server, fake_main)
    old_cm = old_app.lifespan(old_app)
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", old_cm)

    asyncio.run(mcp_server._rebuild_http_mcp_session())

    assert fake_main.router.routes == []
    assert mcp_server._mcp_lifespan_cm is old_cm


def test_guard_is_transparent_for_non_http_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_rebuild(monkeypatch)
    seen: List[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def run() -> None:
        _sent, send = _collector()
        scope: Scope = {"type": "lifespan", "asgi": {"version": "3.0"}}
        await mcp_server.McpHttpGuard(inner)(scope, _receive, send)

    asyncio.run(run())
    assert seen == ["lifespan"]
    assert calls == []


def test_guard_replies_503_when_inner_raises_closed_resource(monkeypatch: pytest.MonkeyPatch) -> None:
    """线上：writer.send ClosedResourceError，SDK 再包一层 ExceptionGroup。"""
    calls = _stub_rebuild(monkeypatch)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        raise anyio.ClosedResourceError

    async def run() -> List[Message]:
        sent, send = _collector()
        await mcp_server.McpHttpGuard(inner)(_scope(), _receive, send)
        return sent

    sent = asyncio.run(run())
    starts = _start_messages(sent)
    assert len(starts) == 1
    assert starts[0]["status"] == 503
    assert calls == [1]


def test_guard_rebuilds_when_inner_raises_after_start(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_rebuild(monkeypatch)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})
        raise anyio.ClosedResourceError

    async def run() -> List[Message]:
        sent, send = _collector()
        await mcp_server.McpHttpGuard(inner)(_scope(), _receive, send)
        return sent

    sent = asyncio.run(run())
    starts = _start_messages(sent)
    assert len(starts) == 1
    assert starts[0]["status"] == 200
    assert calls == [1]


def test_guard_swallows_send_errors_and_rebuilds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_rebuild(monkeypatch)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def boom_send(message: Message) -> None:
        raise ConnectionResetError("peer closed")

    async def run() -> None:
        await mcp_server.McpHttpGuard(inner)(_scope(), _receive, boom_send)

    asyncio.run(run())
    assert calls == [1]


def test_rebuild_skips_when_unmounted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_server = _FakeMcpServer()
    fake_main = _FakeMainApp()
    _patch_rebuild_env(monkeypatch, fake_server, fake_main)
    monkeypatch.setattr(mcp_server, "_http_mounted", False)

    asyncio.run(mcp_server._rebuild_http_mcp_session())

    assert fake_server.apps == []
    assert fake_main.router.routes == []


def test_rebuild_enter_failure_keeps_old_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_server = _FakeMcpServer()
    fake_server.fail_enter = True
    fake_main = _FakeMainApp()
    old_app = _FakeHttpApp()
    _patch_rebuild_env(monkeypatch, fake_server, fake_main)
    old_cm = old_app.lifespan(old_app)
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", old_cm)

    asyncio.run(mcp_server._rebuild_http_mcp_session())

    assert fake_main.router.routes == []
    assert mcp_server._mcp_lifespan_cm is old_cm
