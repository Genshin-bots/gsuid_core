"""MCP Server Streamable HTTP ASGI 运行时、取消隔离与无斜杠挂载原语。"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Callable, Protocol, Awaitable, TypedDict, TypeGuard, runtime_checkable

import anyio
from fastmcp import FastMCP
from starlette.types import Send, Scope, ASGIApp, Message, Receive
from starlette.routing import Match, Mount
from fastmcp.server.http import StarletteWithLifespan
from starlette.responses import JSONResponse
from starlette.applications import Starlette

from gsuid_core.i18n import t
from gsuid_core.logger import logger


class _JsonRpcError(TypedDict):
    code: int
    message: str


class _JsonRpcErrorBody(TypedDict):
    jsonrpc: str
    id: None
    error: _JsonRpcError


def _body_more(message: Message) -> bool:
    """``http.response.body`` / ``http.request`` 是否还有后续块。缺省表示本块结束。"""
    return "more_body" in message and message["more_body"] is True


@runtime_checkable
class _ASGICallable(Protocol):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None: ...


def _is_fresh_asgi(app: object) -> TypeGuard[_ASGICallable]:
    return isinstance(app, _ASGICallable)


def _snapshot_inner(app: ASGIApp) -> object:
    """这一次请求开始时的内层应用。护栏包着 runtime 时，重放不能再打护栏。"""
    return app.current_app() if isinstance(app, _McpHttpRuntime) else app


def _release_task_cancellation() -> None:
    """anyio 取消宿主任务后，不把取消计数留到下一次 await。"""
    task = asyncio.current_task()
    if task is None:
        return
    while task.cancelling():
        task.uncancel()


class _McpHttpRuntime:
    """由常驻任务按顺序进入和退出每代 HTTP lifespan。"""

    def __init__(self, server: FastMCP) -> None:
        self._server = server
        self._app: StarletteWithLifespan | None = None
        self._pending_app: StarletteWithLifespan | None = None
        self._task: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()
        self._ready = asyncio.Event()
        self._error: Exception | None = None
        self._stopping = False

    def current_app(self) -> StarletteWithLifespan | None:
        return self._app

    def _new_http_app(self) -> StarletteWithLifespan:
        return self._server.http_app(path="/", transport="streamable-http", stateless_http=True)

    async def start(self) -> None:
        await self.restart()

    def _spawn(self) -> asyncio.Task[None]:
        # 不继承触发这次恢复的请求上下文。
        return asyncio.create_task(self._run(), name="mcp-http-lifespan", context=contextvars.Context())

    def _drain_task(self) -> None:
        """取走已结束任务的异常，避免 CancelledError 漏给这次请求。"""
        task = self._task
        if task is None or not task.done():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as error:
            logger.debug(t("log.mcp.mcp_server_http_owner_task_fail", e=error))

    async def restart(self) -> None:
        if self._stopping:
            raise RuntimeError("MCP HTTP runtime is stopping")
        self._pending_app = self._new_http_app()
        self._ready.clear()
        if self._task is None or self._task.done():
            self._drain_task()
            self._task = self._spawn()
        else:
            self._changed.set()
        await self._ready.wait()
        if self._stopping:
            raise RuntimeError("MCP HTTP runtime is stopping")
        # 旧一代把宿主任务打死时，同一次 restart 再拉起，不把取消抛给请求。
        if self._task is not None and self._task.done() and self._app is None and self._error is None:
            self._drain_task()
            self._ready.clear()
            self._task = self._spawn()
            await self._ready.wait()
        if self._stopping:
            raise RuntimeError("MCP HTTP runtime is stopping")
        if self._error is not None:
            raise self._error
        if self._task is not None and self._task.done():
            self._drain_task()
        if self._app is None:
            raise RuntimeError("MCP HTTP lifespan did not initialize")

    async def close(self) -> None:
        self._stopping = True
        self._changed.set()
        if self._task is not None:
            if self._app is None and not self._ready.is_set():
                self._task.cancel()
            self._ready.set()
            try:
                await asyncio.shield(self._task)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is None or task.cancelling():
                    raise

    async def _park_until_next(self, error: BaseException | None, *, entered: bool) -> None:
        """进入失败告诉 restart。已进入的一代退出失败时，不挡住待进入的新会话。"""
        self._app = None
        if not entered:
            self._error = (
                error if isinstance(error, Exception) else RuntimeError("MCP HTTP lifespan did not initialize")
            )
            self._ready.set()
            self._changed.clear()
            if self._stopping:
                return
        else:
            self._error = None
            self._ready.clear()
            if self._pending_app is not None or self._stopping:
                return
        try:
            await self._changed.wait()
        except asyncio.CancelledError:
            _release_task_cancellation()
            if not self._stopping:
                raise

    async def _run(self) -> None:
        try:
            while not self._stopping:
                self._ready.clear()
                self._changed.clear()
                entered = False
                scope_cancelled = False
                try:
                    app = self._pending_app
                    self._pending_app = None
                    if app is None:
                        app = self._new_http_app()
                    async with app.lifespan(app):
                        self._app = app
                        self._error = None
                        entered = True
                        self._ready.set()
                        try:
                            await self._changed.wait()
                        except asyncio.CancelledError:
                            # task group 取消会打到宿主。先清计数，退出回调才能跑完。
                            if self._stopping:
                                raise
                            _release_task_cancellation()
                            scope_cancelled = True
                        finally:
                            self._app = None
                except asyncio.CancelledError:
                    _release_task_cancellation()
                    if self._stopping:
                        raise
                    await self._park_until_next(None, entered=entered)
                except BaseExceptionGroup as error:
                    _release_task_cancellation()
                    if self._stopping:
                        break
                    await self._park_until_next(error, entered=entered)
                except Exception as error:
                    if self._stopping:
                        break
                    await self._park_until_next(error, entered=entered)
                else:
                    # 退出没抛错时也不能自己再开一代，要等这次重建交来的新会话。
                    if scope_cancelled:
                        await self._park_until_next(None, entered=True)
        finally:
            self._app = None
            self._ready.set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        app = self._app
        if app is not None:
            await app(scope, receive, send)


class _HttpExchange:
    """一次请求上的收发：扣住未提交的状态码，并记下已读走的请求体。"""

    def __init__(self, receive: Receive, send: Send) -> None:
        self._receive = receive
        self._send = send
        self._recorded: list[Message] = []
        self._replay_at = 0
        self._replaying = False
        self._pending_start: Message | None = None
        self.committed = False
        self.finished = False
        self.client_gone = False

    async def receive(self) -> Message:
        if self._replaying and self._replay_at < len(self._recorded):
            message = self._recorded[self._replay_at]
            self._replay_at += 1
            return message
        # 记录用尽后走原 receive。SSE 要在这里等断开，不能合成 disconnect
        message = await self._receive()
        if not self._replaying:
            self._recorded.append(message)
        return message

    def begin_replay(self) -> None:
        self._replaying = True
        self._replay_at = 0
        self._pending_start = None
        self.committed = False
        self.finished = False

    async def send(self, message: Message) -> None:
        if self.client_gone:
            return
        kind = message["type"]
        if kind == "http.response.start" and not self.committed:
            # 状态码先扣住。毒化请求往往只有 start，还没到客户端就能丢掉并重试。
            self._pending_start = message
            return
        if not self.committed:
            self.committed = True
            start = self._pending_start
            self._pending_start = None
            if start is not None:
                await self._emit(start)
            if self.client_gone:
                return
        await self._emit(message)
        if kind == "http.response.body" and not _body_more(message) and not self.client_gone:
            self.finished = True

    async def _emit(self, message: Message) -> None:
        # 客户端断开时 send 失败不得进 SDK，否则会毒化终身 task group
        try:
            await self._send(message)
        except Exception:
            self.client_gone = True


async def _capture_mcp_failure(run: Callable[[], Awaitable[None]]) -> bool:
    """跑一轮子应用。子应用抛错返回 True；调用方自身取消继续抛出。"""
    caught: BaseException | None = None
    try:
        await run()
    except anyio.get_cancelled_exc_class() as error:
        task = asyncio.current_task()
        if task is None or task.cancelling():
            raise
        caught = error
    except BaseExceptionGroup as error:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise
        caught = error
    except Exception as error:
        caught = error
    if caught is None:
        return False
    logger.warning(t("log.mcp.mcp_server_http_inner_fail", e=caught))
    return True


async def _finish_open_http_response(send: Send) -> None:
    """start 已写出时补一个空 body，让外层 middleware 能结束这次响应。"""
    try:
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    except Exception as e:
        logger.debug(t("log.mcp.mcp_server_http_503_fail", e=e))


async def _respond_mcp_unavailable(scope: Scope, receive: Receive, send: Send) -> None:
    """子应用没写响应时的兜底：显式回 503，别让上层 middleware 抛 RuntimeError。"""
    body: _JsonRpcErrorBody = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32603, "message": "MCP transport unavailable, please retry"},
    }
    response = JSONResponse(body, status_code=503, headers={"Retry-After": "1"})
    await response(scope, receive, send)


async def _close_lifespan_cm(runtime: _McpHttpRuntime | None) -> None:
    """让 lifespan 的所属任务完成退出。"""
    if runtime is None:
        return
    try:
        await runtime.close()
    except Exception as error:
        logger.debug(t("log.mcp.mcp_server_lifespan_exit_fail", e=error))


def _request_route_path(scope: Scope) -> str:
    """Starlette 的 get_route_path 不在公开导出里，这里按同样规则取路由路径。"""
    path = scope["path"]
    if not isinstance(path, str):
        return ""
    root_path = scope["root_path"] if "root_path" in scope else ""
    if not isinstance(root_path, str) or not root_path or not path.startswith(root_path):
        return path
    if path == root_path:
        return ""
    if path[len(root_path)] == "/":
        return path[len(root_path) :]
    return path


def _directory_form(scope: Scope, mount_path: str) -> str:
    """把挂载前缀补成目录形式，保留已有 root_path。"""
    root = scope["root_path"] if "root_path" in scope else ""
    return (root if isinstance(root, str) else "") + mount_path + "/"


def _raw_path_with_directory_slash(raw_path: bytes, old_path: str, new_path: str) -> bytes:
    """raw_path 在 ``?`` 前补斜杠；对不上原 path 时保持原样。"""
    if not old_path.isascii() or not new_path.isascii():
        return raw_path
    old = old_path.encode("ascii")
    new = new_path.encode("ascii")
    if raw_path == old:
        return new
    if raw_path.startswith(old + b"?"):
        return new + raw_path[len(old) :]
    return raw_path


class _McpSlashMount(Mount):
    """同一挂载同时接住无斜杠路径，不发 307。

    客户端跟随 307 会丢掉 Authorization，MCP 初始化变成 401。
    """

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        # 父路由对未匹配路径发 307。匹配前补斜杠，请求不离开原 URL。
        if scope["type"] not in ("http", "websocket") or _request_route_path(scope) != self.path:
            return super().matches(scope)
        old_path = scope["path"]
        if not isinstance(old_path, str):
            return super().matches(scope)
        new_path = _directory_form(scope, self.path)
        old_raw = scope["raw_path"] if "raw_path" in scope else None
        scope["path"] = new_path
        if isinstance(old_raw, (bytes, bytearray)):
            scope["raw_path"] = _raw_path_with_directory_slash(bytes(old_raw), old_path, new_path)
        match, child = super().matches(scope)
        if match == Match.NONE:
            scope["path"] = old_path
            if isinstance(old_raw, (bytes, bytearray)):
                scope["raw_path"] = bytes(old_raw)
        return match, child


def _remove_mounted_path(main_app: Starlette, path: str) -> None:
    """从主 FastAPI 路由表移除指定 mount path。"""
    main_app.router.routes = [r for r in main_app.router.routes if not (isinstance(r, Mount) and r.path == path)]
