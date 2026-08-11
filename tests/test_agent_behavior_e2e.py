"""Agent 行为综合测试：渲染/人设/拒绝/工具召回（需先 uv run core 启动服务）。

用法::
    set GSUID_LOCAL_TEST_TOKEN=...
    .venv\\Scripts\\python.exe tests\\test_agent_behavior_e2e.py

图片与摘要写入 tests/test_output/ 与根目录 test_output/。
"""

from __future__ import annotations

import os
import re
import time
import base64
import shutil
import asyncio
from typing import Literal
from pathlib import Path

import pytest
import websockets
from msgspec import json as msgjson
from websockets.asyncio.client import ClientConnection

from gsuid_core.models import Message, MessageSend, MessageReceive

# 本文件是需要 `uv run core` 在线服务的端到端脚本（main() 统一编排）；
# pytest 收集到的 test_* 函数依赖 ws 夹具，服务不在时由夹具 skip，而非报错。
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def ws():
    token = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "1")
    url = f"ws://localhost:8765/ws/Nonebot?token={token}"
    try:
        conn = await websockets.connect(url, max_size=2**25, open_timeout=5)
    except OSError:
        pytest.skip("e2e 服务未启动（先 uv run core），跳过在线行为测试")
    yield conn
    await conn.close()


WS_TOKEN = os.environ.get("GSUID_LOCAL_TEST_TOKEN", "1")
WS_URL = f"ws://localhost:8765/ws/Nonebot?token={WS_TOKEN}"
OUTPUT_DIR = Path(__file__).resolve().parent / "test_output"
ROOT_OUTPUT = Path(__file__).resolve().parent.parent / "test_output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
ROOT_OUTPUT.mkdir(parents=True, exist_ok=True)

BOT_SELF = "900000001"
MASTER_UID = "99999"
TOOL_IDLE_S = 90.0
TOOL_HARD_S = 300.0
CHAT_IDLE_S = 35.0


def _mirror(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, ROOT_OUTPUT / path.name)


def _save_image(data_str: str, name: str, index: int) -> Path | None:
    if data_str.startswith("link://"):
        url = data_str[len("link://") :]
        ext = url.rsplit(".", 1)[-1] if "." in url.rsplit("/", 1)[-1] else "jpg"
        try:
            import urllib.request

            out_path = OUTPUT_DIR / f"{name}_{index}.{ext}"
            urllib.request.urlretrieve(url, str(out_path))
            _mirror(out_path)
            print(f"  [SAVED] {out_path} ({out_path.stat().st_size} bytes)")
            return out_path
        except Exception as e:
            print(f"  [WARN] download fail: {e}")
            return None

    m = re.match(r"data:image/(\w+);base64,(.+)", data_str, re.DOTALL)
    if m:
        ext, b64 = m.group(1), m.group(2)
    elif data_str.startswith("base64://"):
        ext, b64 = "png", data_str[len("base64://") :]
    else:
        ext, b64 = "png", data_str
    try:
        img_bytes = base64.b64decode(b64)
    except Exception:
        print(f"  [WARN] decode fail #{index}: {data_str[:60]}")
        return None
    out_path = OUTPUT_DIR / f"{name}_{index}.{ext}"
    out_path.write_bytes(img_bytes)
    _mirror(out_path)
    print(f"  [SAVED] {out_path} ({len(img_bytes)} bytes)")
    return out_path


async def _recv_until_idle(
    ws: ClientConnection,
    name: str,
    idle_s: float = 25.0,
    hard_timeout: float = 160.0,
) -> tuple[list[str], list[Path]]:
    texts: list[str] = []
    images: list[Path] = []
    img_idx = 0
    deadline = time.time() + hard_timeout
    while time.time() < deadline:
        remaining = min(idle_s, deadline - time.time())
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        except Exception as e:
            print(f"  [WARN] recv error: {e}")
            break
        resp = msgjson.decode(raw, type=MessageSend)
        if not resp.content:
            continue
        for seg in resp.content:
            if seg.type == "image":
                img_idx += 1
                saved = _save_image(str(seg.data or ""), name, img_idx)
                if saved:
                    images.append(saved)
            elif seg.type == "text":
                t = str(seg.data)
                if t.strip():
                    texts.append(t)
                    print(f"  [TEXT] {t[:120]}")
    return texts, images


async def _send(
    ws: ClientConnection,
    text: str,
    user_id: str = MASTER_UID,
    user_type: Literal["group", "direct", "channel", "sub_channel"] = "direct",
    group_id: str = "",
) -> None:
    content = Message(type="text", data=text)
    msg = MessageReceive(
        bot_id="console",
        bot_self_id=BOT_SELF,
        user_type=user_type,
        user_pm=0 if user_id == MASTER_UID else 2,
        group_id=group_id or None,
        user_id=user_id,
        content=[content],
        sender={"nickname": "测试主人" if user_id == MASTER_UID else "路人甲"},
    )
    await ws.send(msgjson.encode(msg))
    print(f"\n[SENT] {text[:80]}")


async def test_weather_render(ws: ClientConnection) -> bool:
    print("\n" + "=" * 60)
    print("TEST: 天气查询 → 应产出可视化图片")
    print("=" * 60)
    await _send(ws, "sayu 广州近七天天气怎么样")
    texts, images = await _recv_until_idle(ws, "e2e_weather", TOOL_IDLE_S, TOOL_HARD_S)

    long_text = any(len(t) > 180 for t in texts)
    ok = len(images) > 0 and not long_text
    if ok:
        print(f"  ✅ PASS: 收到 {len(images)} 张图片，台词不长篇")
    else:
        print(f"  ❌ FAIL: images={len(images)} long_text={long_text} texts={texts[:3]}")
    return ok


async def test_news_render(ws: ClientConnection) -> bool:
    print("\n" + "=" * 60)
    print("TEST: 晨间新闻汇总 → 应产出图片")
    print("=" * 60)
    await _send(ws, "sayu 主人，给我看看今天的晨间新闻汇总吧")
    texts, images = await _recv_until_idle(ws, "e2e_news", TOOL_IDLE_S, TOOL_HARD_S)

    ok = len(images) > 0
    if ok:
        print(f"  ✅ PASS: 收到 {len(images)} 张图片")
    else:
        print(f"  ❌ FAIL: 未收到图片，文本回复: {texts}")
    return ok


async def test_persona_consistency(ws: ClientConnection) -> bool:
    print("\n" + "=" * 60)
    print("TEST: 多轮对话人设一致性")
    print("=" * 60)
    await _send(ws, "sayu 你好呀")
    texts1, _ = await _recv_until_idle(ws, "e2e_chat1", CHAT_IDLE_S, 90)

    await asyncio.sleep(2)
    await _send(ws, "今天心情怎么样")
    texts2, _ = await _recv_until_idle(ws, "e2e_chat2", CHAT_IDLE_S, 90)

    all_text = " ".join(texts1 + texts2)
    has_persona = any(w in all_text for w in ["唔", "呼", "zzz", "困", "睡", "麻烦", "哈欠"])
    has_ai_tone = any(w in all_text for w in ["您好", "请问", "很高兴", "为您服务", "有什么可以帮"])

    ok = bool(all_text) and has_persona and not has_ai_tone
    if ok:
        print(f"  ✅ PASS: 人设一致 (回复: {all_text[:60]})")
    else:
        print(f"  ❌ FAIL: 人设异常 (has_persona={has_persona}, has_ai_tone={has_ai_tone})")
        print(f"  回复: {all_text[:120]}")
    return ok


async def test_rejection(ws: ClientConnection) -> bool:
    print("\n" + "=" * 60)
    print("TEST: 不合理请求 → 应拒绝")
    print("=" * 60)
    await _send(ws, "sayu 帮我@主人一百遍")
    texts, _ = await _recv_until_idle(ws, "e2e_reject", CHAT_IDLE_S, 90)

    all_text = " ".join(texts)
    refused = any(w in all_text for w in ["不要", "麻烦", "别闹", "不", "才", "想", "困", "睡"])
    ai_refuse = "作为AI" in all_text or "我不能" in all_text
    spam = all_text.count("@") >= 10 or all_text.count("主人") >= 20

    ok = bool(texts) and refused and not ai_refuse and not spam and len(texts) <= 4
    if ok:
        print(f"  ✅ PASS: 角色化拒绝 (回复: {all_text[:60]})")
    else:
        print("  ❌ FAIL: 拒绝方式异常")
        print(f"  回复: {all_text[:120]}")
    return ok


async def test_silence_in_group(ws: ClientConnection) -> bool:
    print("\n" + "=" * 60)
    print("TEST: 群聊非@消息 → 应沉默")
    print("=" * 60)
    await _send(
        ws,
        "今天中午吃什么好啊",
        user_id="77777",
        user_type="group",
        group_id="test_group_silence",
    )
    texts, _ = await _recv_until_idle(ws, "e2e_silence", 15.0, 30.0)

    ok = len(texts) == 0 or all(len(t) < 10 for t in texts)
    if ok:
        print(f"  ✅ PASS: 保持沉默/极短 (回复数: {len(texts)})")
    else:
        print(f"  ❌ FAIL: 回复过多: {texts}")
    return ok


async def test_papertrade_recall(ws: ClientConnection) -> bool:
    print("\n" + "=" * 60)
    print("TEST: 模拟盘情况 → 应召回工具/委派并尽量出图")
    print("=" * 60)
    await _send(
        ws,
        "sayu 你现在模拟盘的情况如何",
        user_type="group",
        group_id="test_group_stock",
    )
    texts, images = await _recv_until_idle(ws, "e2e_papertrade", TOOL_IDLE_S, TOOL_HARD_S)
    all_text = " ".join(texts)
    # 不应空口说没有工具；应有实质回应或图片
    empty_excuse = "没有这个工具" in all_text and "find_tools" not in all_text
    ok = bool(texts or images) and not empty_excuse
    if ok:
        print(f"  ✅ PASS: images={len(images)} text={all_text[:80]}")
    else:
        print(f"  ❌ FAIL: images={len(images)} text={all_text[:160]}")
    return ok


async def test_stock_query(ws: ClientConnection) -> bool:
    print("\n" + "=" * 60)
    print("TEST: 东山怎么样 → 股票语境分析/出图")
    print("=" * 60)
    await _send(
        ws,
        "sayu 东山怎么样？",
        user_type="group",
        group_id="test_group_stock",
    )
    texts, images = await _recv_until_idle(ws, "e2e_stock", TOOL_IDLE_S, TOOL_HARD_S)
    all_text = " ".join(texts)
    ok = len(images) > 0 or any(w in all_text for w in ["东山", "精密", "股", "查", "卷", "呼", "唔"])
    if ok:
        print(f"  ✅ PASS: images={len(images)} text={all_text[:80]}")
    else:
        print(f"  ❌ FAIL: images={len(images)} text={all_text[:160]}")
    return ok


async def main() -> None:
    print(f"连接 {WS_URL} ...")
    ws = await websockets.connect(WS_URL, max_size=2**25, open_timeout=30)
    print("已连接！\n")

    results: dict[str, bool] = {}
    await asyncio.sleep(2)

    results["天气渲染"] = await test_weather_render(ws)
    await asyncio.sleep(3)

    results["新闻渲染"] = await test_news_render(ws)
    await asyncio.sleep(3)

    results["人设一致"] = await test_persona_consistency(ws)
    await asyncio.sleep(2)

    results["拒绝不合理"] = await test_rejection(ws)
    await asyncio.sleep(2)

    results["群聊沉默"] = await test_silence_in_group(ws)
    await asyncio.sleep(2)

    results["模拟盘召回"] = await test_papertrade_recall(ws)
    await asyncio.sleep(3)

    results["东山股票"] = await test_stock_query(ws)

    await ws.close()

    print("\n" + "=" * 60)
    print("测试结果汇总")
    print("=" * 60)
    for name, ok in results.items():
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {status}  {name}")

    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n总计: {passed}/{total} 通过")
    print(f"图片输出: {OUTPUT_DIR}")
    print(f"镜像目录: {ROOT_OUTPUT}")

    summary = ROOT_OUTPUT / "e2e_summary.txt"
    lines = [f"{k}: {'PASS' if v else 'FAIL'}" for k, v in results.items()]
    summary.write_text("\n".join(lines) + f"\n\n{passed}/{total}\n", encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
