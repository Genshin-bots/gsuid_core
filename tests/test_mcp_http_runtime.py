"""MCP HTTP 常驻生命周期：同一任务进出每一代，挂载点保持不变。"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from typing import List
from collections.abc import Callable, Awaitable, AsyncIterator

import anyio
import httpx
import pytest
from fastmcp import FastMCP
from starlette.types import Send, Scope, Message, Receive
from starlette.routing import Mount, Route
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.http import StarletteWithLifespan, StreamableHTTPASGIApp
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.auth.middleware.bearer_auth import RequireAuthMiddleware

from gsuid_core.ai_core.mcp import server as mcp_server


def _scope() -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
    }


def _one_shot_body(body: bytes) -> tuple[List[int], Receive]:
    reads: List[int] = []

    async def receive() -> Message:
        reads.append(1)
        if len(reads) > 1:
            raise RuntimeError("original receive called after the body was consumed")
        return {"type": "http.request", "body": body, "more_body": False}

    return reads, receive


def _patch_host(monkeypatch: pytest.MonkeyPatch, server: FastMCP, host: Starlette) -> None:
    monkeypatch.setattr("gsuid_core.app_life.app", host)
    monkeypatch.setattr(mcp_server, "_mcp_server", server)
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", None)
    monkeypatch.setattr(mcp_server, "_mcp_mount_path", None)
    monkeypatch.setattr(mcp_server, "_server_task", None)
    monkeypatch.setattr(mcp_server, "_http_mounted", False)
    monkeypatch.setattr(mcp_server, "_mcp_last_rebuild", 0.0)
    monkeypatch.setattr(mcp_server, "_mcp_rebuild_lock", asyncio.Lock())


def _mounts(host: Starlette) -> List[Mount]:
    return [route for route in host.routes if isinstance(route, Mount)]


def test_shutdown_owns_initial_lifespan_before_it_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FastMCP("synthetic-initial-shutdown")
    host = Starlette()
    _patch_host(monkeypatch, server, host)

    async def run() -> None:
        entered, exited = asyncio.Event(), asyncio.Event()

        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            try:
                entered.set()
                await asyncio.Event().wait()
                yield
            finally:
                exited.set()

        def factory(*, path: str, transport: str, stateless_http: bool) -> StarletteWithLifespan:
            return StarletteWithLifespan(lifespan=lifespan)

        monkeypatch.setattr(server, "http_app", factory)
        starting = asyncio.create_task(mcp_server._mount_http_mcp(server, "/api/mcp"))
        await entered.wait()
        runtime = mcp_server._mcp_lifespan_cm
        try:
            await asyncio.wait_for(mcp_server._shutdown_mcp_server(), 1)
            with pytest.raises(RuntimeError, match="stopping"):
                await asyncio.wait_for(starting, 1)
            assert exited.is_set()
            assert runtime is not None and runtime._task is not None and runtime._task.done()
            assert host.routes == []
            assert not mcp_server._http_mounted
            assert mcp_server._mcp_lifespan_cm is None
            assert mcp_server._mcp_server is None
        finally:
            starting.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await starting
            if runtime is not None:
                await runtime.close()

    asyncio.run(run())


def test_shutdown_can_stop_a_rebuild_waiting_inside_its_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FastMCP("synthetic-rebuild-shutdown")
    host = Starlette()
    _patch_host(monkeypatch, server, host)

    async def run() -> None:
        entered, exited = asyncio.Event(), asyncio.Event()
        generation = 0

        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            current_generation = generation
            try:
                if current_generation == 2:
                    entered.set()
                    await asyncio.Event().wait()
                yield
            finally:
                if current_generation == 2:
                    exited.set()

        def factory(*, path: str, transport: str, stateless_http: bool) -> StarletteWithLifespan:
            nonlocal generation
            generation += 1
            return StarletteWithLifespan(lifespan=lifespan)

        monkeypatch.setattr(server, "http_app", factory)
        await mcp_server._mount_http_mcp(server, "/api/mcp")
        runtime = mcp_server._mcp_lifespan_cm
        rebuilding = asyncio.create_task(mcp_server._rebuild_http_mcp_session())
        await entered.wait()
        try:
            await asyncio.wait_for(mcp_server._shutdown_mcp_server(), 1)
            await asyncio.wait_for(rebuilding, 1)
            assert exited.is_set()
            assert runtime is not None and runtime._task is not None and runtime._task.done()
            assert host.routes == []
            assert not mcp_server._mcp_rebuild_lock.locked()
            assert not mcp_server._http_mounted
            assert mcp_server._mcp_lifespan_cm is None
            assert mcp_server._mcp_server is None
        finally:
            rebuilding.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await rebuilding
            if runtime is not None:
                await runtime.close()

    asyncio.run(run())


def test_cancelled_mount_caller_clears_owner_and_allows_next_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FastMCP("synthetic-cancelled-mount")
    host = Starlette()
    _patch_host(monkeypatch, server, host)

    async def run() -> None:
        entered, exited = asyncio.Event(), asyncio.Event()
        generation = 0

        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            current_generation = generation
            try:
                if current_generation == 1:
                    entered.set()
                    await asyncio.Event().wait()
                yield
            finally:
                if current_generation == 1:
                    exited.set()

        def factory(*, path: str, transport: str, stateless_http: bool) -> StarletteWithLifespan:
            nonlocal generation
            generation += 1
            return StarletteWithLifespan(lifespan=lifespan)

        monkeypatch.setattr(server, "http_app", factory)
        starting = asyncio.create_task(mcp_server._mount_http_mcp(server, "/api/mcp"))
        await entered.wait()
        runtime = mcp_server._mcp_lifespan_cm
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, 1)
        try:
            assert exited.is_set()
            assert runtime is not None and runtime._task is not None and runtime._task.done()
            assert mcp_server._mcp_lifespan_cm is None
            assert not mcp_server._http_mounted
            assert host.routes == []
            await asyncio.wait_for(mcp_server._mount_http_mcp(server, "/api/mcp"), 1)
            assert mcp_server._http_mounted
            assert len(host.routes) == 1
        finally:
            await mcp_server._shutdown_mcp_server()

    asyncio.run(run())


def test_lifespans_enter_and_exit_in_the_same_owner_without_request_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("synthetic_request", default="clean")
    events: List[tuple[str, int, str]] = []
    server = FastMCP("synthetic-owner")

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        task = asyncio.current_task()
        assert task is not None
        assert marker.get() == "clean"
        token = marker.set("owner")
        async with anyio.create_task_group():
            events.append(("enter", id(task), marker.get()))
            try:
                yield
            finally:
                current = asyncio.current_task()
                assert current is not None
                events.append(("exit", id(current), marker.get()))
                marker.reset(token)

    def factory(*, path: str, transport: str, stateless_http: bool) -> StarletteWithLifespan:
        assert marker.get() == "caller"
        return StarletteWithLifespan(lifespan=lifespan)

    monkeypatch.setattr(server, "http_app", factory)

    async def run() -> None:
        token = marker.set("caller")
        runtime = mcp_server._McpHttpRuntime(server)
        try:
            await runtime.start()
            await asyncio.create_task(runtime.restart())
            await runtime.close()
            assert marker.get() == "caller"
        finally:
            marker.reset(token)

    asyncio.run(run())
    assert [event[0] for event in events] == ["enter", "exit", "enter", "exit"]
    assert len({event[1] for event in events}) == 1


def test_stopping_during_lifespan_entry_finishes_without_an_orphan(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FastMCP("synthetic-start-stop")

    async def run() -> None:
        entered = asyncio.Event()
        exited = asyncio.Event()

        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            try:
                entered.set()
                await asyncio.Event().wait()
                yield
            finally:
                exited.set()

        def factory(*, path: str, transport: str, stateless_http: bool) -> StarletteWithLifespan:
            return StarletteWithLifespan(lifespan=lifespan)

        monkeypatch.setattr(server, "http_app", factory)
        runtime = mcp_server._McpHttpRuntime(server)
        starting = asyncio.create_task(runtime.start())
        await entered.wait()
        await asyncio.wait_for(runtime.close(), 1)
        with pytest.raises(RuntimeError, match="stopping"):
            await starting
        assert exited.is_set()
        assert runtime._task is not None and runtime._task.done()

    asyncio.run(run())


def test_rebuild_restarts_session_without_remounting(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FastMCP("synthetic-remount")
    host = Starlette()
    _patch_host(monkeypatch, server, host)
    generations: List[int] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        generations.append(1)
        try:
            yield
        finally:
            generations.append(-1)

    def factory(*, path: str, transport: str, stateless_http: bool) -> StarletteWithLifespan:
        return StarletteWithLifespan(lifespan=lifespan)

    monkeypatch.setattr(server, "http_app", factory)

    async def run() -> None:
        await mcp_server._mount_http_mcp(server, "/api/mcp")
        try:
            guard = _mounts(host)[0].app
            await mcp_server._rebuild_http_mcp_session()
            await mcp_server._rebuild_http_mcp_session()
            assert _mounts(host)[0].app is guard
            assert len(_mounts(host)) == 1
        finally:
            await mcp_server._shutdown_mcp_server()

    asyncio.run(run())
    # 挂载进入，第一次重建退出再进入，关闭时退出。冷却内的第二次不进入。
    assert generations == [1, -1, 1, -1]


def test_rebuild_factory_failure_keeps_old_session(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FastMCP("synthetic-factory-fail")
    host = Starlette()
    _patch_host(monkeypatch, server, host)
    fail = False
    entered: List[int] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        entered.append(1)
        try:
            yield
        finally:
            entered.append(-1)

    def factory(*, path: str, transport: str, stateless_http: bool) -> StarletteWithLifespan:
        if fail:
            raise RuntimeError("http_app boom")
        return StarletteWithLifespan(lifespan=lifespan)

    monkeypatch.setattr(server, "http_app", factory)

    async def run() -> None:
        nonlocal fail
        await mcp_server._mount_http_mcp(server, "/api/mcp")
        runtime = mcp_server._mcp_lifespan_cm
        assert runtime is not None
        live = runtime.current_app()
        fail = True
        try:
            await mcp_server._rebuild_http_mcp_session()
            assert runtime.current_app() is live
            assert entered == [1]
            assert len(_mounts(host)) == 1
        finally:
            await mcp_server._shutdown_mcp_server()

    asyncio.run(run())


def test_rebuild_enter_failure_exits_old_session_and_keeps_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FastMCP("synthetic-enter-fail")
    host = Starlette()
    _patch_host(monkeypatch, server, host)
    fail_enter = False
    events: List[tuple[str, int]] = []

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        task = asyncio.current_task()
        assert task is not None
        events.append(("enter", id(task)))
        if fail_enter:
            raise RuntimeError("lifespan boom")
        try:
            yield
        finally:
            current = asyncio.current_task()
            assert current is not None
            events.append(("exit", id(current)))

    def factory(*, path: str, transport: str, stateless_http: bool) -> StarletteWithLifespan:
        return StarletteWithLifespan(lifespan=lifespan)

    monkeypatch.setattr(server, "http_app", factory)

    async def run() -> None:
        nonlocal fail_enter
        await mcp_server._mount_http_mcp(server, "/api/mcp")
        runtime = mcp_server._mcp_lifespan_cm
        assert runtime is not None
        fail_enter = True
        try:
            await mcp_server._rebuild_http_mcp_session()
            assert runtime.current_app() is None
            assert len(_mounts(host)) == 1
            assert [event[0] for event in events] == ["enter", "exit", "enter"]
            fail_enter = False
            mcp_server._mcp_last_rebuild = 0.0
            await mcp_server._rebuild_http_mcp_session()
            assert runtime.current_app() is not None
            assert [event[0] for event in events] == ["enter", "exit", "enter", "enter"]
            assert len({event[1] for event in events}) == 1
        finally:
            await mcp_server._shutdown_mcp_server()

    asyncio.run(run())


def test_rebuild_skips_when_unmounted(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FastMCP("synthetic-unmounted")
    calls: List[int] = []

    def factory(*, path: str, transport: str, stateless_http: bool) -> StarletteWithLifespan:
        calls.append(1)
        return StarletteWithLifespan()

    monkeypatch.setattr(server, "http_app", factory)
    runtime = mcp_server._McpHttpRuntime(server)
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", runtime)
    monkeypatch.setattr(mcp_server, "_http_mounted", False)
    monkeypatch.setattr(mcp_server, "_mcp_last_rebuild", 0.0)
    monkeypatch.setattr(mcp_server, "_mcp_rebuild_lock", asyncio.Lock())

    asyncio.run(mcp_server._rebuild_http_mcp_session())
    assert calls == []


class _BodyApp:
    """只发 start，或发完整 body。runtime 只要求 lifespan 和 ASGI 调用。"""

    def __init__(self, *, finish: bool, body: bytes) -> None:
        self.finish = finish
        self.body = body
        self.calls = 0

    @contextlib.asynccontextmanager
    async def _cm(self) -> AsyncIterator[None]:
        yield

    def lifespan(self, _app: _BodyApp) -> contextlib.AbstractAsyncContextManager[None]:
        return self._cm()

    async def __call__(self, _scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if self.finish:
            await send({"type": "http.response.body", "body": self.body, "more_body": False})


def test_guard_replays_the_new_inner_app_in_the_same_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """状态码还没写出时，重放打新的内层应用，不把 runtime 自己再包一层。"""
    server = FastMCP("synthetic-replay")
    host = Starlette()
    _patch_host(monkeypatch, server, host)
    made: List[_BodyApp] = []

    def factory(*, path: str, transport: str, stateless_http: bool) -> _BodyApp:
        app = _BodyApp(finish=len(made) > 0, body=b"ok")
        made.append(app)
        return app

    monkeypatch.setattr(server, "http_app", factory)
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/call"}'
    reads, receive = _one_shot_body(body)

    async def run() -> List[Message]:
        await mcp_server._mount_http_mcp(server, "/api/mcp")
        try:
            guard = _mounts(host)[0].app
            assert isinstance(guard, mcp_server.McpHttpGuard)
            sent: List[Message] = []

            async def send(message: Message) -> None:
                sent.append(message)

            await guard(_scope(), receive, send)
            assert _mounts(host)[0].app is guard
            return sent
        finally:
            await mcp_server._shutdown_mcp_server()

    sent = asyncio.run(run())
    starts = [message for message in sent if message["type"] == "http.response.start"]
    bodies = [message["body"] for message in sent if message["type"] == "http.response.body"]
    assert len(starts) == 1
    assert starts[0]["status"] == 200
    assert bodies == [b"ok"]
    assert reads == [1]
    assert [app.calls for app in made] == [1, 1]
    assert len(made) == 2


class _SyntheticVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        if token != "synthetic-local-only":
            return None
        return AccessToken(
            token=token,
            client_id="synthetic",
            scopes=["mcp:tools"],
            claims={"user_id": "synthetic"},
        )


def test_real_sdk_keeps_auth_and_recovers_after_a_closed_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """关闭的 reader 在重放期间仍然生效，所以这一次是 503，下一次请求恢复。"""
    from mcp.server.models import InitializationOptions
    from mcp.shared.message import SessionMessage
    from starlette.requests import Request
    from starlette.responses import Response
    from anyio.streams.memory import MemoryObjectSendStream, MemoryObjectReceiveStream

    async def passthrough(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        return await call_next(request)

    async def run() -> None:
        host = Starlette()
        host.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
        server = FastMCP("synthetic-real-sdk", auth=_SyntheticVerifier())
        calls: List[int] = []

        @server.tool
        def ping() -> str:
            calls.append(1)
            return "synthetic-only"

        _patch_host(monkeypatch, server, host)
        await mcp_server._mount_http_mcp(server, "/api/mcp")
        runtime = mcp_server._mcp_lifespan_cm
        assert runtime is not None
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer synthetic-local-only",
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        }
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host, raise_app_exceptions=False),
                base_url="http://synthetic.local",
            ) as client:
                denied = await asyncio.wait_for(
                    client.post("/api/mcp/", headers={"Accept": headers["Accept"]}, json=payload),
                    15,
                )
                assert denied.status_code == 401
                ok = await asyncio.wait_for(client.post("/api/mcp/", headers=headers, json=payload), 15)
                assert ok.status_code == 200
                assert calls == [1]

                async def closed_reader(
                    _read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
                    _write_stream: MemoryObjectSendStream[SessionMessage],
                    _initialization_options: InitializationOptions,
                    _raise_exceptions: bool = False,
                    _stateless: bool = False,
                ) -> None:
                    return None

                with monkeypatch.context() as patch:
                    patch.setattr(server._mcp_server, "run", closed_reader)
                    failed = await asyncio.wait_for(client.post("/api/mcp/", headers=headers, json=payload), 15)
                    assert failed.status_code == 503
                assert calls == [1]
                again = await asyncio.wait_for(client.post("/api/mcp/", headers=headers, json=payload), 15)
                assert again.status_code == 200
                assert calls == [1, 1]
                denied_again = await asyncio.wait_for(
                    client.post("/api/mcp/", headers={"Accept": headers["Accept"]}, json=payload),
                    15,
                )
                assert denied_again.status_code == 401
        finally:
            await runtime.close()
        assert runtime._task is not None and runtime._task.done()

    asyncio.run(run())


def _live_session_manager(app: Starlette) -> StreamableHTTPSessionManager:
    for route in app.routes:
        if not isinstance(route, Route):
            continue
        endpoint: object = route.endpoint
        if isinstance(endpoint, RequireAuthMiddleware):
            endpoint = endpoint.app
        if isinstance(endpoint, StreamableHTTPASGIApp):
            manager = endpoint.session_manager
            if isinstance(manager, StreamableHTTPSessionManager):
                return manager
    raise AssertionError("streamable session manager was not mounted")


def test_next_request_succeeds_after_task_group_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    """世代已经退出后，下一次 POST 重建会话并得到工具结果。"""
    from starlette.requests import Request
    from starlette.responses import Response

    async def passthrough(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        return await call_next(request)

    async def run() -> None:
        host = Starlette()
        host.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
        server = FastMCP("synthetic-task-group-cancel", auth=_SyntheticVerifier())
        calls: List[int] = []

        @server.tool
        def ping() -> str:
            calls.append(1)
            return "synthetic-only"

        _patch_host(monkeypatch, server, host)
        await mcp_server._mount_http_mcp(server, "/api/mcp")
        runtime = mcp_server._mcp_lifespan_cm
        assert runtime is not None
        try:
            live = runtime.current_app()
            assert isinstance(live, Starlette)
            manager = _live_session_manager(live)
            group = manager._task_group
            assert group is not None

            async def poison() -> None:
                raise RuntimeError("synthetic task group poison")

            group.start_soon(poison)
            for _ in range(200):
                if runtime.current_app() is None:
                    break
                await asyncio.sleep(0.01)
            assert runtime.current_app() is None
            headers = {
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer synthetic-local-only",
            }
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {}},
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host, raise_app_exceptions=False),
                base_url="http://synthetic.local",
            ) as client:
                recovered = await asyncio.wait_for(
                    client.post("/api/mcp/", headers=headers, json=payload),
                    15,
                )
            assert recovered.status_code == 200
            assert "synthetic-only" in recovered.text
            assert calls == [1]
            assert runtime._task is not None and not runtime._task.done()
        finally:
            await mcp_server._shutdown_mcp_server()

    asyncio.run(run())


def test_in_flight_post_finishes_and_next_request_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具已在跑时 SDK 往往已写出响应头。护栏不重放这次请求，但结束块要补上，下一次 POST 恢复。"""
    from starlette.requests import Request
    from starlette.responses import Response

    async def passthrough(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        return await call_next(request)

    async def run() -> None:
        host = Starlette()
        host.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
        server = FastMCP("synthetic-in-flight-cancel", auth=_SyntheticVerifier())
        calls: List[int] = []
        entered = asyncio.Event()

        @server.tool
        async def ping() -> str:
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                await asyncio.Event().wait()
            return "synthetic-only"

        _patch_host(monkeypatch, server, host)
        await mcp_server._mount_http_mcp(server, "/api/mcp")
        runtime = mcp_server._mcp_lifespan_cm
        assert runtime is not None
        try:
            live = runtime.current_app()
            assert isinstance(live, Starlette)
            manager = _live_session_manager(live)
            group = manager._task_group
            assert group is not None
            headers = {
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer synthetic-local-only",
            }
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {}},
            }
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=host, raise_app_exceptions=False),
                base_url="http://synthetic.local",
            ) as client:
                posting = asyncio.create_task(client.post("/api/mcp/", headers=headers, json=payload))
                await asyncio.wait_for(entered.wait(), 5)

                async def poison() -> None:
                    raise RuntimeError("synthetic in-flight task group poison")

                group.start_soon(poison)
                first = await asyncio.wait_for(posting, 15)
                assert first.status_code != 500
                recovered = await asyncio.wait_for(client.post("/api/mcp/", headers=headers, json=payload), 15)
            assert recovered.status_code == 200
            assert "synthetic-only" in recovered.text
            assert len(calls) >= 2
            assert runtime._task is not None and not runtime._task.done()
        finally:
            await mcp_server._shutdown_mcp_server()

    asyncio.run(run())
