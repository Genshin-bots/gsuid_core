"""MCP 多租户凭据 e2e 校验（手动脚本）。

对 127.0.0.1:8766 的测试实例模拟 e2e_bot 平台，跑通：
私聊绑定/列表/解绑、群绑定拒绝、双用户隔离（未绑不回落共享 key）、
群聊调用私有工具输出打码、LLM 上下文无原始凭据（配合日志核查）。

凭据从 data/ai_core/mcp_configs/luckin_coffee.json 现读，脚本只打尾号。
"""

import sys
import json
import time
import asyncio
from pathlib import Path

import websockets
from msgspec import json as msgjson

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from gsuid_core.models import Message, MessageSend, MessageReceive

WS = "ws://127.0.0.1:8766/ws/e2e_bot?token=e2e_test_token"
GROUP = "g_multi"
UA, UB = "user_alice", "user_bob"

cfg = json.load(open("data/ai_core/mcp_configs/luckin_coffee.json", encoding="utf-8"))
REAL_TOKEN = cfg["env"]["LUCKIN_MCP_TOKEN"]
print(f"[setup] luckin token tail=...{REAL_TOKEN[-4:]}")


def mk(text: str, uid: str, group: bool, at_bot: bool = False) -> bytes:
    content = [Message(type="at", data="e2e_self")] if at_bot else []
    content.append(Message(type="text", data=text))
    return msgjson.encode(
        MessageReceive(
            bot_id="e2e_bot",
            bot_self_id="e2e_self",
            user_type="group" if group else "direct",
            user_pm=5,
            group_id=GROUP if group else None,
            user_id=uid,
            content=content,
        )
    )


async def send(ws, text: str, uid: str, group: bool, wait: float = 20.0, at_bot: bool = False) -> list[str]:
    await ws.send(mk(text, uid, group, at_bot))
    out: list[str] = []
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            break
        try:
            m = msgjson.decode(raw, type=MessageSend)
            for c in m.content or []:
                if c.type == "text" and isinstance(c.data, str):
                    out.append(c.data)
        except Exception:
            pass
    return out


def show(tag: str, lines: list[str]) -> None:
    safe = [ln.replace(REAL_TOKEN, "<TOKEN>") for ln in lines]
    print(f"\n=== {tag} ===")
    for ln in safe:
        print(ln[:400] or "(empty)")
    if not safe:
        print("(no reply)")


async def main() -> None:
    async with websockets.connect(WS, max_size=2**25, open_timeout=15) as ws:
        # T1 群绑定拒绝
        show("T1 group bind rejected", await send(ws, f"mcp绑定 Luckin Coffee {REAL_TOKEN}", UA, True))
        # T2 Alice 私聊裸命令 → 可绑清单
        show("T2 dm bare bind list", await send(ws, "mcp绑定", UA, False))
        # T3 Alice 私聊绑定真实 token（验活+落库）
        show("T3 alice dm bind real", await send(ws, f"mcp绑定 Luckin Coffee {REAL_TOKEN}", UA, False, 45))
        # T4 Alice 绑定列表
        show("T4 alice bind list", await send(ws, "mcp绑定列表", UA, False))
        # T5 Bob 绑定列表 → 空（隔离：看不到 Alice 的）
        show("T5 bob bind list empty", await send(ws, "mcp绑定列表", UB, False))
        # T6 Bob 私聊调用私有工具意图 → 应得 BIND_GUIDE 而非共享 key 执行
        show("T6 bob dm luckin query", await send(ws, "帮我查一下瑞幸咖啡的门店列表", UB, False, 60))
        # T7 Alice 群里 @bot 调用（已绑）→ 允许执行，输出应打码
        show(
            "T7 alice group @bot luckin query",
            await send(ws, "帮我查一下瑞幸咖啡的门店列表", UA, True, 90, at_bot=True),
        )
        # T7b Bob 群里 @bot 调用（未绑）→ BIND_GUIDE
        show(
            "T7b bob group @bot luckin query",
            await send(ws, "帮我查一下瑞幸咖啡的门店列表", UB, True, 90, at_bot=True),
        )
        # T8 Alice 解绑
        show("T8 alice unbind", await send(ws, "mcp解绑 Luckin Coffee", UA, False))
        # T9 解绑后再调 → BIND_GUIDE
        show("T9 alice dm after unbind", await send(ws, "帮我查一下瑞幸咖啡的门店列表", UA, False, 60))


asyncio.run(main())
