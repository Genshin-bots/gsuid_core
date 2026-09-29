"""实机复现：Starlette GZipMiddleware 对流式 SSE 响应的小块数据的缓冲行为。

跑法（不要用 uv run，venv 里有进程在跑时 uv run 会失败）：
    .venv\\Scripts\\python.exe eval/manual/repro_gzip_sse.py

对同一段 SSE 流量分别过「GZipMiddleware」和「不过」，逐块记录下游收到字节的时刻，
用来证明：压缩路径下小块数据被 gzip 内部缓冲吞掉，直到攒够才成批吐出。
"""

import time
import asyncio
from typing import Any

import starlette
from starlette.types import Send, Scope, Message, Receive
from starlette.responses import StreamingResponse
from starlette.middleware.gzip import GZipMiddleware

# 模拟一条真实日志的 SSE data 行（id 行在生成器里按序号拼）
DATA_LINE = (
    '{"level": "INFO", "message": "\u65e5\u5fd7\u5185\u5bb9", '
    '"message_type": "html", "timestamp": "09-30 10:00:00", "plugin": "SayuCore"}'
)
SSE_FRAME = f"id: 12345\ndata: {DATA_LINE}\n\n"
KEEPALIVE = ": keepalive\n\n"
N_EVENTS = 200
GAP = 0.005  # 模拟每条日志的到达间隔


async def sse_app(scope: Scope, receive: Receive, send: Send) -> None:
    async def gen():
        for i in range(N_EVENTS):
            await asyncio.sleep(GAP)
            yield f"id: {i}\ndata: {DATA_LINE}\n\n"
        await asyncio.sleep(0.1)
        yield KEEPALIVE

    resp = StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
    await resp(scope, receive, send)


def make_probe(inner: Any) -> tuple[Send, list[tuple[float, int]]]:
    """返回一个 send：记录每个下游 body 块的 (时刻, 字节数)，并过滤掉 0 字节空块。"""
    t0 = time.monotonic()
    arrivals: list[tuple[float, int]] = []

    async def probe_send(message: Message) -> None:
        if message["type"] == "http.response.body":
            body = message.get("body", b"")
            arrivals.append((time.monotonic() - t0, len(body)))
        await inner(message)

    return probe_send, arrivals


async def drive(app: Any) -> list[tuple[float, int]]:
    """以 SSE 的 scope 跑一次 app，收集下游收到的每个 body 块。"""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/logs/stream",
        "raw_path": b"/api/logs/stream",
        "query_string": b"level=all",
        "root_path": "",
        "headers": [(b"accept-encoding", b"gzip, deflate, br"), (b"accept", b"text/event-stream")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8765),
    }
    sent: list[Message] = []

    async def drain_send(message: Message) -> None:
        sent.append(message)

    probe_send, arrivals = make_probe(drain_send)
    await app(scope, lambda: asyncio.sleep(3600), probe_send)
    return arrivals


def report(title: str, arrivals: list[tuple[float, int]]) -> None:
    nonempty = [(t, n) for t, n in arrivals if n > 0]
    total = sum(n for _, n in nonempty)
    print(f"\n=== {title} ===")
    print(f"  下游收到 body 块总数 : {len(arrivals)}  (其中 0 字节空块 {len(arrivals) - len(nonempty)} 个)")
    print(f"  真正带数据的块        : {len(nonempty)}")
    print(f"  实际传出的字节        : {total}")
    if nonempty:
        print(f"  首块到达时刻          : {nonempty[0][0]:.3f}s  ({nonempty[0][1]} B)")
        print(f"  末块到达时刻          : {nonempty[-1][0]:.3f}s  ({nonempty[-1][1]} B)")
        print(f"  前 5 块               : {[(round(t, 3), n) for t, n in nonempty[:5]]}")
        print(f"  后 5 块               : {[(round(t, 3), n) for t, n in nonempty[-5:]]}")
        if len(nonempty) > 1:
            print(f"  首块→末块跨度         : {nonempty[-1][0] - nonempty[0][0]:.3f}s")
            avg = (nonempty[-1][0] - nonempty[0][0]) / (len(nonempty) - 1)
            print(f"  平均块间隔            : {avg:.4f}s")
    print(f"  是否实时(块数≈事件数) : {len(nonempty) >= N_EVENTS * 0.9}")


async def main() -> None:
    print(f"starlette GZipMiddleware 实测  |  源帧 {len(SSE_FRAME)} B / 条，事件 {N_EVENTS} 条，间隔 {GAP}s")
    print(f"starlette 版本: {starlette.__version__}（>=0.46 起跳过 text/event-stream，见 #2871）")

    plain = await drive(sse_app)
    report("A. 不加压缩（基线）", plain)

    gz_arrivals = await drive(GZipMiddleware(sse_app, minimum_size=500, compresslevel=6))
    report("B. starlette GZipMiddleware（当前线上行为）", gz_arrivals)

    def nonempty(xs: list[tuple[float, int]]) -> int:
        return len([1 for _, n in xs if n > 0])

    print("\n--- 结论 ---")
    base, got = nonempty(plain), nonempty(gz_arrivals)
    print(f"带数据块数  基线={base}  实际={got}")
    if got >= base * 0.9:
        print("SSE 逐帧实时送达，issue #283 未复现。")
    else:
        print("警告：SSE 被攒批了！starlette 可能被降级到 0.46 之前，请检查 uv.lock。")


if __name__ == "__main__":
    asyncio.run(main())
