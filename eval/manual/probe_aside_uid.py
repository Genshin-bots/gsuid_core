"""实机：两个私聊用户先后发送 gs查询可莉，核对源码路径和记忆路径。"""

from __future__ import annotations

import time
import asyncio
from pathlib import Path

import websockets
from msgspec import json as msgjson

from gsuid_core.models import Message, MessageSend, MessageReceive

PORT = 8765
TOKEN = "1"
BOT = "aside-e2e"
HINT = "你还没有绑定过uid"


def _frame(user_id: str) -> bytes:
    msg = MessageReceive(
        bot_id=BOT,
        bot_self_id="aside-bot",
        msg_id=f"m-{user_id}-{int(time.time())}",
        user_type="direct",
        user_id=user_id,
        content=[Message(type="text", data="gs查询可莉")],
    )
    return msgjson.encode(msg)


def _texts(raw: bytes | str) -> list[str]:
    data = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    try:
        send = msgjson.decode(data, type=MessageSend)
    except Exception:
        return []
    if not send.content:
        return []
    out: list[str] = []
    for part in send.content:
        if part.type == "text" and isinstance(part.data, str) and part.data.strip():
            out.append(part.data.strip())
    return out


async def _round(ws: websockets.ClientConnection, user_id: str, wait_s: float) -> list[str]:
    await ws.send(_frame(user_id))
    got: list[str] = []
    deadline = time.monotonic() + wait_s
    grace: float | None = None
    while time.monotonic() < deadline:
        remain = deadline - time.monotonic()
        timeout = min(15, remain)
        if grace is not None:
            timeout = min(timeout, max(0.1, grace - time.monotonic()))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            if grace is not None and time.monotonic() >= grace:
                break
            continue
        if isinstance(raw, bytes):
            got.extend(_texts(raw))
        else:
            got.extend(_texts(raw))
        advice = [text for text in got if HINT not in text and len(text) > 8]
        if any(HINT in text for text in got) and advice and grace is None:
            grace = time.monotonic() + 8
        if grace is not None and time.monotonic() >= grace:
            break
    return got


def _dump(label: str, lines: list[str]) -> None:
    path = Path(r"F:\gsuid_core\data\aside_e2e_out.txt")
    block = label + "\n" + "\n".join(line.replace("\n", " | ") for line in lines) + "\n"
    prev = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(prev + block, encoding="utf-8")
    print(block, flush=True)


def _reset_out() -> None:
    Path(r"F:\gsuid_core\data\aside_e2e_out.txt").write_text("", encoding="utf-8")


async def main() -> None:
    _reset_out()
    url = f"ws://127.0.0.1:{PORT}/ws/{BOT}?token={TOKEN}"
    async with websockets.connect(url, max_size=2**25, open_timeout=30) as ws:
        first = await _round(ws, "aside_user_o", 180)
        _dump("--- USER O ---", first)
        second = await _round(ws, "aside_user_p", 180)
        _dump("--- USER P ---", second)
    print("logs", Path(r"F:\gsuid_core\data\ai_core\session_logs"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
