"""McpHttpGuard：MCP HTTP 子应用护栏（隔离上层取消 + 未写出响应时同请求重试）。

回归的是线上事故：stateless Streamable HTTP 的终身 task group 被子任务取消毒化后，
子应用只写出响应头就返回。状态码还没到客户端时，护栏应重建并重放这一次请求。
"""

from __future__ import annotations

import json
import asyncio
from typing import List

import anyio
import pytest
from starlette.types import Send, Scope, Message, Receive

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


class _ReplayOwner:
    """测试替身：只提供 current_app()，护栏重放走生产替换路径。"""

    def __init__(self, app: object | None = None) -> None:
        self.app = app

    def current_app(self) -> object | None:
        return self.app


def _stub_rebuild(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    calls: List[int] = []

    async def fake() -> None:
        calls.append(1)

    monkeypatch.setattr(mcp_server, "_rebuild_http_mcp_session", fake)
    # 不留下别的测试换上的常驻任务，否则未提交失败会被误重放
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", None)
    return calls


def _start_messages(sent: List[Message]) -> List[Message]:
    return [message for message in sent if message["type"] == "http.response.start"]


def test_guard_forwards_normal_response(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_rebuild(monkeypatch)
    sent, send = _collector()

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
        await send({"type": "http.response.body", "body": b"b"})

    asyncio.run(mcp_server.McpHttpGuard(inner)(_scope(), _receive, send))
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
    ]
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


def test_child_cancellation_returns_503_without_swallowing_requester_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """子应用自己抛出的取消要回 503。调用方取消任务时，护栏把取消继续抛出。"""
    rebuilds: List[int] = []

    async def rebuild() -> None:
        rebuilds.append(1)

    async def child_cancel(scope: Scope, receive: Receive, send: Send) -> None:
        raise asyncio.CancelledError("synthetic child cancellation")

    monkeypatch.setattr(mcp_server, "_rebuild_http_mcp_session", rebuild)
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", None)

    async def run() -> None:
        sent, send = _collector()
        await mcp_server.McpHttpGuard(child_cancel)(_scope(), _receive, send)
        assert [message["status"] for message in _start_messages(sent)] == [503]
        assert rebuilds == [1]
        rebuilds.clear()
        started = asyncio.Event()

        async def waiting(scope: Scope, receive: Receive, send: Send) -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(mcp_server.McpHttpGuard(waiting)(_scope(), _receive, send))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rebuilds == []

    asyncio.run(run())


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


def test_guard_closes_body_and_rebuilds_when_stream_never_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """线上：SSE 分支吞掉 BrokenResourceError，start 已写、body 没有结束。"""
    calls = _stub_rebuild(monkeypatch)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data:", "more_body": True})

    async def run() -> List[Message]:
        sent, send = _collector()
        await mcp_server.McpHttpGuard(inner)(_scope(), _receive, send)
        return sent

    sent = asyncio.run(run())
    starts = _start_messages(sent)
    assert len(starts) == 1
    assert starts[0]["status"] == 200
    bodies = [message for message in sent if message["type"] == "http.response.body"]
    assert bodies[-1]["body"] == b""
    assert bodies[-1]["more_body"] is False
    assert calls == [1]


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


def test_guard_swallows_send_errors_without_rebuilding(monkeypatch: pytest.MonkeyPatch) -> None:
    """写出失败且子应用正常返回时，会话还在，不重建。"""
    calls = _stub_rebuild(monkeypatch)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def boom_send(message: Message) -> None:
        raise ConnectionResetError("peer closed")

    async def run() -> None:
        await mcp_server.McpHttpGuard(inner)(_scope(), _receive, boom_send)

    asyncio.run(run())
    assert calls == []


def _one_shot_body(body: bytes) -> tuple[List[int], Receive]:
    """第一次返回 body；再读原通道就失败。重放必须用记录，不能再调原 receive。"""
    calls: List[int] = []

    async def receive() -> Message:
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("original receive called after the body was consumed")
        return {"type": "http.request", "body": body, "more_body": False}

    return calls, receive


def test_guard_retries_same_request_after_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    """线上 500：只写出 start 就返回。状态码还没到客户端，应重放并只交付第二次结果。"""
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/call"}'
    reads, receive = _one_shot_body(body)
    rebuilds: List[int] = []
    seen: List[bytes] = []
    owner = _ReplayOwner()
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", owner)

    async def healed(scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        raw = message["body"]
        assert isinstance(raw, bytes)
        seen.append(raw)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def poisoned(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def fake() -> None:
        rebuilds.append(1)
        owner.app = healed

    monkeypatch.setattr(mcp_server, "_rebuild_http_mcp_session", fake)

    async def run() -> List[Message]:
        sent, send = _collector()
        await mcp_server.McpHttpGuard(poisoned)(_scope(), receive, send)
        return sent

    sent = asyncio.run(run())
    starts = _start_messages(sent)
    assert len(starts) == 1
    assert starts[0]["status"] == 200
    bodies = [message["body"] for message in sent if message["type"] == "http.response.body"]
    assert bodies == [b"ok"]
    assert seen == [body]
    assert reads == [1]
    assert rebuilds == [1]


def test_guard_retries_when_another_request_already_rebuilt(monkeypatch: pytest.MonkeyPatch) -> None:
    """冷却期内重建是空操作，但新子应用已经换上时，这次请求仍要重放。"""
    reads, receive = _one_shot_body(b"{}")
    rebuilds: List[int] = []

    async def healed(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def poisoned(scope: Scope, receive: Receive, send: Send) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def fake() -> None:
        rebuilds.append(1)

    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", _ReplayOwner(healed))
    monkeypatch.setattr(mcp_server, "_rebuild_http_mcp_session", fake)

    async def run() -> List[Message]:
        sent, send = _collector()
        await mcp_server.McpHttpGuard(poisoned)(_scope(), receive, send)
        return sent

    sent = asyncio.run(run())
    bodies = [message["body"] for message in sent if message["type"] == "http.response.body"]
    assert bodies == [b"ok"]
    assert len(_start_messages(sent)) == 1
    assert reads == [1]
    assert rebuilds == [1]


def test_guard_replies_503_when_replay_also_stays_uncommitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """重放仍只写出 start 时回 503，并且只重建一次。"""
    rebuilds: List[int] = []
    replayed: List[int] = []
    owner = _ReplayOwner()
    monkeypatch.setattr(mcp_server, "_mcp_lifespan_cm", owner)

    async def still_bad(scope: Scope, receive: Receive, send: Send) -> None:
        replayed.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def poisoned(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def fake() -> None:
        rebuilds.append(1)
        owner.app = still_bad

    monkeypatch.setattr(mcp_server, "_rebuild_http_mcp_session", fake)

    async def run() -> List[Message]:
        sent, send = _collector()
        await mcp_server.McpHttpGuard(poisoned)(_scope(), _receive, send)
        return sent

    sent = asyncio.run(run())
    starts = _start_messages(sent)
    assert len(starts) == 1
    assert starts[0]["status"] == 503
    assert replayed == [1]
    assert rebuilds == [1]
