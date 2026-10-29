"""MCP 挂载同时接受 /api/mcp 与 /api/mcp/，不把无斜杠请求 307 走。"""

from __future__ import annotations

import asyncio
from typing import List, TypedDict

import httpx
from starlette.types import Send, Scope, Receive
from starlette.responses import PlainTextResponse
from starlette.applications import Starlette

from gsuid_core.ai_core.mcp.server import (
    _McpSlashMount,
    _remove_mounted_path,
    _raw_path_with_directory_slash,
)


class _Seen(TypedDict):
    path: str
    root: str
    query: bytes
    raw_path: bytes
    authorization: bytes


def _authorization(scope: Scope) -> bytes:
    headers = scope["headers"]
    for key, value in headers:
        if key == b"authorization":
            return value
    return b""


def _host() -> tuple[Starlette, List[_Seen]]:
    seen: List[_Seen] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        path = scope["path"]
        root = scope["root_path"] if "root_path" in scope else ""
        query = scope["query_string"] if "query_string" in scope else b""
        raw_path = scope["raw_path"] if "raw_path" in scope else b""
        assert isinstance(path, str)
        assert isinstance(root, str)
        assert isinstance(query, bytes)
        assert isinstance(raw_path, bytes)
        seen.append(
            {
                "path": path,
                "root": root,
                "query": query,
                "raw_path": raw_path,
                "authorization": _authorization(scope),
            }
        )
        await PlainTextResponse("ok")(scope, receive, send)

    host = Starlette()
    host.router.routes.append(_McpSlashMount("/api/mcp", app=inner))
    return host, seen


def test_bare_and_slashed_mcp_paths_hit_the_same_app() -> None:
    host, seen = _host()

    async def run() -> None:
        transport = httpx.ASGITransport(app=host)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            bare = await client.post("/api/mcp", headers={"Authorization": "Bearer canvas-key"})
            slashed = await client.post("/api/mcp/", headers={"Authorization": "Bearer canvas-key"})
            queried = await client.post("/api/mcp?x=1", headers={"Authorization": "Bearer canvas-key"})
            other = await client.post("/api/other")

        assert bare.status_code == 200
        assert "location" not in bare.headers
        assert slashed.status_code == 200
        assert queried.status_code == 200
        assert "location" not in queried.headers
        assert other.status_code == 404
        assert [item["path"] for item in seen] == ["/api/mcp/", "/api/mcp/", "/api/mcp/"]
        assert [item["root"] for item in seen] == ["/api/mcp", "/api/mcp", "/api/mcp"]
        assert seen[0]["authorization"] == b"Bearer canvas-key"
        assert seen[1]["authorization"] == b"Bearer canvas-key"
        assert seen[2]["query"] == b"x=1"
        assert seen[2]["raw_path"] == b"/api/mcp/"
        assert seen[0]["raw_path"] == b"/api/mcp/"

    asyncio.run(run())


def test_raw_path_slash_keeps_query_string() -> None:
    slashed = _raw_path_with_directory_slash(b"/api/mcp?x=1", "/api/mcp", "/api/mcp/")
    assert slashed == b"/api/mcp/?x=1"
    untouched = _raw_path_with_directory_slash(b"/api/mcp/", "/nope", "/api/mcp/")
    assert untouched == b"/api/mcp/"


def test_unmount_removes_slash_mount() -> None:
    host, _seen = _host()
    assert len(host.routes) == 1
    _remove_mounted_path(host, "/api/mcp")
    assert host.routes == []
