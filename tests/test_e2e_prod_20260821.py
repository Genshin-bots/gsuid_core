"""生产复盘 E2E：长结构出图、工具召回、人设不污染。对运行中的 core 打 WS。

群号 / 用户号只从环境读，不写进仓库。
"""

from __future__ import annotations

import os
import re
import sys
import time
import base64
import shutil
import asyncio
from typing import Literal
from pathlib import Path

import websockets
from PIL import Image
from msgspec import json as msgjson

from gsuid_core.models import Message, MessageSend, MessageReceive

WS_TOKEN = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "1")
WS_URL = f"ws://localhost:8765/ws/Nonebot?token={WS_TOKEN}"
OUT = Path(__file__).resolve().parent / "test_output"
ROOT = Path(__file__).resolve().parent.parent / "test_output"
OUT.mkdir(exist_ok=True)
ROOT.mkdir(exist_ok=True)

BOT_SELF = os.environ.get("GSUID_E2E_BOT_SELF", "900000001")
USER_ID = os.environ.get("GSUID_E2E_USER_ID", "99999")
GROUP_ID = os.environ.get("GSUID_E2E_GROUP_ID", "")
ONLY = {s.strip() for s in os.environ.get("GSUID_E2E_ONLY", "").split(",") if s.strip()}
SPAN_Q = os.environ.get("GSUID_E2E_SPAN_Q", "sayu 帮我整理近七日的信息对照")
DIGEST_Q = os.environ.get("GSUID_E2E_DIGEST_Q", "sayu 帮我汇总一下今天的要点")


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


def image_has_visual_structure(path: Path) -> bool:
    """不是纯文字墙：颜色种类和边缘变化要够。"""
    im = Image.open(path).convert("RGB")
    w, h = im.size
    if w < 80 or h < 80:
        return False
    sample = im.resize((80, 80))
    colors = {px for px in sample.getdata()}
    if len(colors) < 18:
        return False
    # 水平扫描：相邻像素差，图表/卡片会有色带
    diffs = 0
    pixels = list(sample.getdata())
    for i in range(1, len(pixels)):
        a, b = pixels[i - 1], pixels[i]
        if abs(a[0] - b[0]) + abs(a[1] - b[1]) + abs(a[2] - b[2]) > 40:
            diffs += 1
    return diffs > 80


async def recv(
    ws,
    name: str,
    *,
    expect_target: str,
    expect_image: bool = False,
    idle: float = 28.0,
    hard: float = 300.0,
):
    texts: list[str] = []
    images: list[Path] = []
    idx, end = 0, time.time() + hard
    last_match = time.time()
    got_match = False
    while time.time() < end:
        wait = 150.0 if (expect_image and not images) else idle
        timeout = min(wait, end - time.time())
        if timeout <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        except Exception as e:
            print(f"  [ws] {type(e).__name__}: {e}", flush=True)
            break
        resp = msgjson.decode(raw, type=MessageSend)
        if resp.target_id and str(resp.target_id) != str(expect_target):
            if got_match and time.time() - last_match >= wait:
                break
            continue
        if not resp.content:
            continue
        got_match = True
        last_match = time.time()
        for seg in resp.content:
            if seg.type == "image":
                idx += 1
                p = _save(str(seg.data or ""), name, idx)
                if p:
                    images.append(p)
            elif seg.type == "text" and str(seg.data).strip():
                texts.append(str(seg.data))
                print(f"  [TEXT] {str(seg.data)[:160]}", flush=True)
    return texts, images


def _msg(
    text: str,
    *,
    group_id: str | None,
    user_type: Literal["group", "direct"],
) -> MessageReceive:
    return MessageReceive(
        bot_id="console",
        bot_self_id=BOT_SELF,
        user_type=user_type,
        user_pm=0,
        group_id=group_id,
        user_id=USER_ID,
        content=[Message(type="text", data=text)],
        sender={"nickname": "测试主人"},
    )


async def scene(ws, name: str, text: str, *, group_id: str | None, expect_image: bool) -> dict[str, object]:
    if ONLY and name not in ONLY:
        print(f"\n[SKIP] {name}", flush=True)
        return {"name": name, "images": 0, "visual": 0, "texts": [], "long_speech": False, "ok": True}
    user_type = "group" if group_id else "direct"
    await ws.send(msgjson.encode(_msg(text, group_id=group_id, user_type=user_type)))
    print(f"\n[SENT] {name}: {text}", flush=True)
    target = group_id or USER_ID
    texts, images = await recv(ws, name, expect_target=str(target), expect_image=expect_image)
    visual = [p for p in images if image_has_visual_structure(p)]
    long_speech = any(len(t) > 180 for t in texts)
    result = {
        "name": name,
        "images": len(images),
        "visual": len(visual),
        "texts": texts[:4],
        "long_speech": long_speech,
        "ok": (len(visual) > 0) if expect_image else True,
    }
    print(
        f"  [{name}] imgs={len(images)} visual={len(visual)} long_speech={long_speech} "
        f"{'PASS' if result['ok'] else 'FAIL'}",
        flush=True,
    )
    return result


async def main() -> None:
    print("connect", WS_URL)
    ws = await websockets.connect(WS_URL, max_size=2**25, open_timeout=30)
    gid = GROUP_ID or None
    results: list[dict[str, object]] = []
    results.append(await scene(ws, "span_info", SPAN_Q, group_id=gid, expect_image=True))
    results.append(await scene(ws, "digest", DIGEST_Q, group_id=gid, expect_image=True))
    await ws.close()
    failed = [r["name"] for r in results if not r["ok"]]
    print("\n==== SUMMARY ====")
    for r in results:
        print(r["name"], "PASS" if r["ok"] else "FAIL", "imgs", r["images"], "visual", r["visual"])
    if failed:
        raise SystemExit(f"failed: {failed}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    asyncio.run(main())
