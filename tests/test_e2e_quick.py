"""快速 e2e：天气 + 闲聊 + 拒绝（服务需已就绪）。"""

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


async def recv(ws, name: str, idle=50.0, hard=180.0):
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
                print(f"  [TEXT] {str(seg.data)[:120]}")
    return texts, images


async def send(ws, text: str, user_type="direct", group_id=None, user_id="99999"):
    msg = MessageReceive(
        bot_id="console",
        bot_self_id="3399214199",
        user_type=user_type,  # type: ignore[arg-type]
        user_pm=0,
        group_id=group_id,
        user_id=user_id,
        content=[Message(type="text", data=text)],
        sender={"nickname": "Wuyi测试"},
    )
    await ws.send(msgjson.encode(msg))
    print(f"\n[SENT] {text}")


async def main():
    print("connect", WS_URL)
    ws = await websockets.client.connect(WS_URL, max_size=2**25, open_timeout=30)
    results = {}

    await send(ws, "sayu 广州近七天天气怎么样")
    t, imgs = await recv(ws, "quick_weather")
    results["天气"] = len(imgs) > 0
    print("天气", "PASS" if results["天气"] else "FAIL", "imgs", len(imgs), "texts", t[:2])

    await asyncio.sleep(2)
    await send(ws, "sayu 你好呀")
    t, _ = await recv(ws, "quick_chat", idle=25, hard=60)
    all_t = " ".join(t)
    results["人设"] = any(w in all_t for w in ["唔", "呼", "睡", "困", "zzz", "麻烦"]) and "您好" not in all_t
    print("人设", "PASS" if results["人设"] else "FAIL", all_t[:80])

    await asyncio.sleep(1)
    await send(ws, "sayu 帮我@主人一百遍")
    t, _ = await recv(ws, "quick_reject", idle=25, hard=60)
    all_t = " ".join(t)
    results["拒绝"] = bool(t) and all_t.count("主人") < 20 and "作为AI" not in all_t
    print("拒绝", "PASS" if results["拒绝"] else "FAIL", all_t[:80])

    await ws.close()
    print("\n===", sum(results.values()), "/", len(results), "===")
    for k, v in results.items():
        print(("PASS" if v else "FAIL"), k)


if __name__ == "__main__":
    asyncio.run(main())
