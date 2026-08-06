"""长链路 e2e：天气委派出图 → 追问是否认框架为用户 → 第二任务再委派 subagent。

判定：
1. 天气：至少 1 张图 + 角色短句
2. 追问（明天带伞吗）：有文本回复；不得出现「系统/框架/任务完成」当群友的口吻
3. 第二任务（晨间新闻/近三日大事汇总）：应再次 create_subagent 或出图
   （通过后续 session log 校验 tool 序列）
"""

from __future__ import annotations

import os
import re
import json
import time
import base64
import shutil
import asyncio
from pathlib import Path

import websockets.client
from msgspec import json as msgjson

from gsuid_core.models import Message, MessageSend, MessageReceive

WS_TOKEN = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "KLtc5aJxG4NrpiG7fSnvUsGRILSP5u5Q5QqqPIwhfjk")
WS_URL = f"ws://localhost:8765/ws/Nonebot?token={WS_TOKEN}"
OUT = Path(__file__).resolve().parent / "test_output"
ROOT = Path(__file__).resolve().parent.parent / "test_output"
LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "ai_core" / "session_logs"
OUT.mkdir(exist_ok=True)
ROOT.mkdir(exist_ok=True)

# 追问泄漏模式：把框架注入当群友
_LEAK_RE = re.compile(
    r"叫系统的|系统说|框架[·・]任务|刚刚那个叫|子任务异步|任务编号\s*#?\d+|交付包",
    re.I,
)


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


async def recv(ws, name: str, idle=100.0, hard=480.0):
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
                t = str(seg.data)
                # 过滤定时任务/推送噪声
                if t.startswith("⏰") or "雪球" in t[:20] or "scheduled_task" in t:
                    continue
                texts.append(t)
                print(f"  [TEXT] {t[:180]}")
    return texts, images


async def send(ws, text: str, user_id: str = "99999"):
    msg = MessageReceive(
        bot_id="console",
        bot_self_id="3399214199",
        user_type="direct",
        user_pm=0,
        group_id=None,
        user_id=user_id,
        content=[Message(type="text", data=text)],
        sender={"nickname": "Wuyi测试"},
    )
    await ws.send(msgjson.encode(msg))
    print(f"\n[SENT] {text}")


def _latest_session_log() -> Path | None:
    files = sorted(LOG_DIR.glob("*99999*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _analyze_session(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries") or []
    tools: list[str] = []
    inj = 0
    user_fw = 0
    create_n = 0
    for e in entries:
        t = e.get("type")
        d = e.get("data") or {}
        if t == "system_injection":
            inj += 1
        if t == "user_input":
            c = str(d.get("content") or "")
            if "[框架·" in c or "[系统·子任务" in c:
                user_fw += 1
        if t == "tool_call":
            name = d.get("tool_name") or ""
            tools.append(name)
            if name == "create_subagent":
                create_n += 1
    return {
        "file": path.name,
        "system_injection": inj,
        "user_input_framework_leak": user_fw,
        "create_subagent_count": create_n,
        "tools": tools,
    }


async def main() -> None:
    print("connect", WS_URL)
    ws = await websockets.client.connect(WS_URL, max_size=2**25, open_timeout=30)
    results: dict[str, bool] = {}

    # ── Turn 1: 天气 → 委派 render ──
    await send(ws, "sayu 帮我查一下上海未来五天的天气，做成图给我")
    t1, imgs1 = await recv(ws, "chain_weather", idle=120.0, hard=480.0)
    results["T1_天气出图"] = len(imgs1) > 0
    results["T1_有台词"] = len(t1) > 0
    print(
        "T1",
        "PASS" if results["T1_天气出图"] else "FAIL",
        "imgs",
        len(imgs1),
        "texts",
        t1[:2],
    )

    # 等回灌收尾落稳，再追问（避免锁竞争误判）
    await asyncio.sleep(8)

    # ── Turn 2: 追问，检验是否把框架当用户 ──
    await send(ws, "sayu 那明天要带伞吗？就一句话")
    t2, imgs2 = await recv(ws, "chain_followup", idle=60.0, hard=180.0)
    all2 = " ".join(t2)
    results["T2_有回复"] = len(t2) > 0
    results["T2_无框架泄漏"] = (not _LEAK_RE.search(all2)) if t2 else False
    # 应基于上文天气简答，不应再完整重做七天（允许短答）
    results["T2_短答"] = len(all2) < 400 if t2 else False
    print(
        "T2",
        "PASS" if all(results[k] for k in results if k.startswith("T2_")) else "FAIL",
        all2[:120],
    )

    await asyncio.sleep(5)

    # ── Turn 3: 新长任务，必须还能 create_subagent ──
    await send(ws, "sayu 帮我汇总一下今天科技圈的三条要闻，做成信息图")
    t3, imgs3 = await recv(ws, "chain_news", idle=120.0, hard=480.0)
    results["T3_有产出"] = len(imgs3) > 0 or len(t3) > 0
    results["T3_出图优先"] = len(imgs3) > 0
    print(
        "T3",
        "PASS" if results["T3_有产出"] else "FAIL",
        "imgs",
        len(imgs3),
        "texts",
        t3[:2],
    )

    await ws.close()

    # ── Session log 结构校验 ──
    await asyncio.sleep(3)
    logp = _latest_session_log()
    if logp:
        stats = _analyze_session(logp)
        print("LOG", stats)
        results["LOG_system_injection"] = stats["system_injection"] >= 1
        results["LOG_no_user_fw"] = stats["user_input_framework_leak"] == 0
        results["LOG_multi_subagent"] = stats["create_subagent_count"] >= 1
        # 理想：两次任务各至少一次 create_subagent
        results["LOG_subagent_ge2"] = stats["create_subagent_count"] >= 2
    else:
        print("LOG missing")
        results["LOG_system_injection"] = False
        results["LOG_no_user_fw"] = False
        results["LOG_multi_subagent"] = False
        results["LOG_subagent_ge2"] = False

    print("\n===", sum(results.values()), "/", len(results), "===")
    for k, v in results.items():
        print(("PASS" if v else "FAIL"), k)

    # 硬门槛：T1 出图、T2 无泄漏有回复、T3 有产出、log 无 user 框架泄漏
    hard = [
        "T1_天气出图",
        "T2_有回复",
        "T2_无框架泄漏",
        "T3_有产出",
        "LOG_no_user_fw",
    ]
    if not all(results.get(k) for k in hard):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
