"""单测：天气查询必须出图（长等待，覆盖异步委派）。"""

from __future__ import annotations

import os
import re
import time
import base64
import shutil
import asyncio
from pathlib import Path

import websockets.client
from msgspec import json as msgjson

from gsuid_core.models import Message, MessageSend, MessageReceive

WS_TOKEN = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "1")
WS_URL = f"ws://localhost:8765/ws/Nonebot?token={WS_TOKEN}"
OUT = Path(__file__).resolve().parent / "test_output"
ROOT = Path(__file__).resolve().parent.parent / "test_output"
OUT.mkdir(exist_ok=True)
ROOT.mkdir(exist_ok=True)


def _save(data: str, name: str, idx: int) -> Path | None:
    if data.startswith("link://"):
        url = data[7:]
        p = OUT / f"{name}_{idx}.jpg"
        try:
            import urllib.request

            urllib.request.urlretrieve(url, str(p))
            shutil.copy2(p, ROOT / p.name)
            print(f"  [SAVED] {p}")
            return p
        except Exception as e:
            print("  download fail", e)
            return None
    m = re.match(r"data:image/(\w+);base64,(.+)", data, re.DOTALL)
    if m:
        ext, b64 = m.group(1), m.group(2)
    elif data.startswith("base64://"):
        ext, b64 = "png", data[9:]
    else:
        ext, b64 = "png", data
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    p = OUT / f"{name}_{idx}.{ext}"
    p.write_bytes(raw)
    shutil.copy2(p, ROOT / p.name)
    print(f"  [SAVED] {p} ({len(raw)}B)")
    return p


async def recv(ws, name: str, idle=90.0, hard=420.0):
    texts, images = [], []
    idx, end = 0, time.time() + hard
    while time.time() < end:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(idle, end - time.time()))
        except asyncio.TimeoutError:
            break
        resp = msgjson.decode(raw, type=MessageSend)
        if not resp.content:
            continue
        for seg in resp.content:
            if seg.type == "image":
                idx += 1
                p = _save(str(seg.data or ""), name, idx)
                if p:
                    images.append(p)
            elif seg.type == "text" and str(seg.data).strip():
                texts.append(str(seg.data))
                print(f"  [TEXT] {str(seg.data)[:160]}")
    return texts, images


async def main() -> None:
    print("connect", WS_URL)
    ws = await websockets.client.connect(WS_URL, max_size=2**25, open_timeout=30)
    msg = MessageReceive(
        bot_id="console",
        bot_self_id="900000001",
        user_type="direct",
        user_pm=0,
        group_id=None,
        user_id="99999",
        content=[Message(type="text", data="sayu 广州近七天天气怎么样")],
        sender={"nickname": "测试主人"},
    )
    await ws.send(msgjson.encode(msg))
    print("\n[SENT] sayu 广州近七天天气怎么样")
    texts, images = await recv(ws, "weather_only")
    await ws.close()
    ok = len(images) > 0
    print("天气", "PASS" if ok else "FAIL", "imgs", len(images), "texts", texts[:3])
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
