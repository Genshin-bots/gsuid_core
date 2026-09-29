"""回归：全局 GZipMiddleware 不得压缩/缓冲 SSE，否则实时日志会成批延迟送达（issue #283）。

starlette < 0.46 的 GZipMiddleware 用 GzipFile 的内部缓冲压缩流式响应，对
``more_body=True`` 的中间块不 flush——每一帧 SSE 都被吞成 0 字节空块，直到流关闭
（或攒够 zlib 内部窗口）才成批到达，网页控制台表现为「隔几分钟刷一批」。

0.46.0 (#2871) 起上游会跳过 ``text/event-stream``，1.5.0 (#3419) 又补了逐块 flush。
修复方案是升级依赖而非自建旁路中间件（见 uv.lock：fastapi 0.142.0 / starlette 1.7.0），
所以本文件的作用变成**钉住这个上游行为**：依赖一旦被降级回去，这里立刻红。
"""

from __future__ import annotations

import gzip
import asyncio
from collections.abc import AsyncGenerator

import starlette
from starlette.types import Send, Scope, ASGIApp, Message, Receive
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.middleware.gzip import GZipMiddleware

FRAME_COUNT = 20
FRAME = b'data: {"level": "INFO", "message": "\xe6\x97\xa5\xe5\xbf\x97"}\n\n'

# GZipMiddleware 开始跳过 text/event-stream 的 starlette 版本（#2871）。
SSE_SKIP_SINCE = (0, 46, 0)

# 浏览器给 EventSource 也会带这个头，且 JS 侧无法去掉：复现条件与真实场景一致。
SSE_REQUEST_HEADERS = [(b"accept-encoding", b"gzip, deflate, br")]


def _sse_scope() -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/logs/stream",
        "raw_path": b"/api/logs/stream",
        "query_string": b"",
        "root_path": "",
        "headers": SSE_REQUEST_HEADERS,
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 8765),
    }


async def _sse_app(scope: Scope, receive: Receive, send: Send) -> None:
    """模拟 /api/logs/stream：逐帧 yield 的 StreamingResponse。"""

    async def gen() -> AsyncGenerator[bytes, None]:
        for _ in range(FRAME_COUNT):
            yield FRAME

    await StreamingResponse(gen(), media_type="text/event-stream")(scope, receive, send)


async def _drive(app: ASGIApp) -> list[Message]:
    captured: list[Message] = []
    never = asyncio.Event()

    async def send_wrap(message: Message) -> None:
        captured.append(message)

    async def receive_wrap() -> Message:
        # StreamingResponse 会另起 task 循环等 http.disconnect；这里必须挂住而不是立刻返回，
        # 否则它空转成死循环。等正文发完，task group 自己会 cancel 掉这个协程。
        await never.wait()
        return {"type": "http.disconnect"}

    await app(_sse_scope(), receive_wrap, send_wrap)
    return captured


def _headers(message: Message) -> dict[bytes, bytes]:
    raw = message.get("headers", [])
    return {name: value for name, value in raw}


def _bodies(messages: list[Message]) -> list[bytes]:
    return [m.get("body", b"") for m in messages if m["type"] == "http.response.body"]


def _parse(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def test_starlette_is_new_enough_to_skip_sse() -> None:
    """锁依赖的护栏：starlette 低于 0.46 会重新引入 issue #283。"""
    assert _parse(starlette.__version__) >= SSE_SKIP_SINCE, (
        f"starlette {starlette.__version__} 会压缩 SSE，需 >= {'.'.join(str(p) for p in SSE_SKIP_SINCE)}"
    )


def test_sse_is_not_compressed_and_every_frame_is_delivered() -> None:
    """SSE 不带 Content-Encoding，且每一帧都原样实时送达。"""
    messages = asyncio.run(_drive(GZipMiddleware(_sse_app, minimum_size=500, compresslevel=6)))

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert b"content-encoding" not in _headers(start)

    bodies = _bodies(messages)
    nonempty = [b for b in bodies if b]
    assert len(nonempty) == FRAME_COUNT
    assert all(chunk == FRAME for chunk in nonempty)


def test_non_sse_response_is_still_compressed() -> None:
    """普通大响应继续走 gzip，SSE 跳过不能误伤常规压缩。"""
    payload = b"x" * 4096

    async def plain_app(scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse({"k": payload.decode()})(scope, receive, send)

    messages = asyncio.run(_drive(GZipMiddleware(plain_app, minimum_size=500, compresslevel=6)))

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert _headers(start).get(b"content-encoding") == b"gzip"
    assert payload in gzip.decompress(b"".join(_bodies(messages)))


def test_small_response_is_left_uncompressed() -> None:
    """低于 minimum_size 的小响应不压，和上游行为一致。"""

    async def small_app(scope: Scope, receive: Receive, send: Send) -> None:
        await PlainTextResponse("hi")(scope, receive, send)

    messages = asyncio.run(_drive(GZipMiddleware(small_app, minimum_size=500, compresslevel=6)))

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert b"content-encoding" not in _headers(start)
    assert b"".join(_bodies(messages)) == b"hi"


def test_non_sse_streaming_response_keeps_start_before_body() -> None:
    """非 SSE 的流式响应（CSV/JSONL 导出）压缩后仍须先发 start。

    start 里才有 Content-Encoding / Content-Length，先发 body 会让 ASGI 服务器报错或截断。
    """
    chunk = b"a" * 600

    async def export_app(scope: Scope, receive: Receive, send: Send) -> None:
        async def gen() -> AsyncGenerator[bytes, None]:
            for _ in range(5):
                yield chunk

        await StreamingResponse(gen(), media_type="text/csv")(scope, receive, send)

    messages = asyncio.run(_drive(GZipMiddleware(export_app, minimum_size=500, compresslevel=6)))

    types = [m["type"] for m in messages]
    assert types[0] == "http.response.start"
    assert types.count("http.response.start") == 1
    assert all(t == "http.response.body" for t in types[1:])

    start = messages[0]
    assert _headers(start).get(b"content-encoding") == b"gzip"
    assert b"content-length" not in _headers(start)
    assert gzip.decompress(b"".join(_bodies(messages))) == chunk * 5


def test_already_encoded_response_is_not_recompressed() -> None:
    """已有 Content-Encoding（如预压缩静态资源）保持原样。"""

    async def encoded_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream"), (b"content-encoding", b"gzip")],
            }
        )
        await send({"type": "http.response.body", "body": FRAME})

    messages = asyncio.run(_drive(GZipMiddleware(encoded_app, minimum_size=500, compresslevel=6)))

    assert _bodies(messages) == [FRAME]
