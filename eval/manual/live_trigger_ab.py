"""实机 A/B：向运行中的 core 发一批消息，抓日志里的 [命令触发] 记录。

用于验证「索引化后命令匹配行为完全没变」。脚本只发消息 + 收集触发记录，
不做断言 —— 结论靠 A/B 两次跑出来的结果 diff。

用法：
    uv run python eval/manual/live_trigger_ab.py send <tag>
    uv run python eval/manual/live_trigger_ab.py collect <tag>
"""

from __future__ import annotations

import re
import sys
import json
import asyncio
from pathlib import Path

import websockets
from msgspec import json as msgjson

URL = "ws://127.0.0.1:8765/ws/AbTestBot"
OUT = Path(__file__).resolve().parents[2] / "eval" / "manual" / "_ab_out"

# 覆盖：命中、参数、空格容忍（半角/全角/NBSP）、纯英文、兄弟命令共享前缀、
# 长消息、纯闲聊、纯数字、英文 ping
CORPUS: tuple[str, ...] = (
    "帮助",
    " 帮助",
    "  帮助  ",
    "原神帮助",
    "原神 帮助",
    "原神  帮助",
    "原神　帮助",
    "原神\xa0帮助",
    "原神帮助 温迪",
    "原神抽卡记录 90 温迪",
    "原神抽卡记录90",
    "原神角色",
    "原神角色 温迪",
    "原神角色列表 温迪",
    "gs帮助",
    "gs 帮助",
    "崩铁帮助",
    "崩铁 帮助",
    "鸣潮共鸣",
    "鸣潮 共鸣",
    "共鸣",
    "status",
    "help",
    "help me",
    "unsend list",
    "这里有个关键词",
    "今天天气真不错啊大家吃了吗",
    "完全无关的一串闲聊内容",
    "原神帮助" * 20,
    "在吗在吗在吗",
    "1",
    "12345",
    "test",
    "ping",
)


async def send(tag: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    uri = URL + "?token=1"
    async with websockets.connect(uri, max_size=None) as ws:
        for i, text in enumerate(CORPUS):
            payload = {
                "bot_id": "AbTestBot",
                "bot_self_id": "99999",
                "msg_id": f"ab-{tag}-{i}",
                "user_type": "group",
                "group_id": "99000001",
                "user_id": "99000001",
                "sender": {"nickname": "ab", "user_id": 99000001},
                "user_pm": 0,
                "content": [{"type": "text", "data": text}],
            }
            await ws.send(msgjson.encode(payload))
            await asyncio.sleep(0.3)
    (OUT / f"sent_{tag}.txt").write_text("\n".join(CORPUS), encoding="utf-8")
    print(f"已发送 {len(CORPUS)} 条，tag={tag}")


def collect(tag: str) -> None:
    """抽出本轮每条消息实际命中了哪些触发器，规整成「消息序号 -> keyword」便于 diff。

    日志里 `command=` 存的是 get_command 之后的 Event repr，其中
    `msg_id='ab-TAG-N'` 定位消息、内层 `command='...'` 就是命中的触发器关键字。
    """
    log_dir = Path("data/logs")
    files = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not files:
        print("找不到日志")
        return
    per_msg: dict[int, set[str]] = {}
    for f in files:
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            if f"ab-{tag}-" not in line or "命令触发" not in line:
                continue
            blob = line.split("] ", 1)[-1].strip()
            try:
                obj = json.loads(blob)
            except json.JSONDecodeError:
                continue
            cmd = obj.get("command")
            if not isinstance(cmd, str):
                continue
            idx_m = re.search(rf"ab-{tag}-(\d+)", cmd)
            kw_m = re.search(r"command='([^']*)'", cmd)
            if idx_m is None or kw_m is None:
                continue
            per_msg.setdefault(int(idx_m.group(1)), set()).add(kw_m.group(1))
    lines: list[str] = []
    for idx in sorted(per_msg):
        text = CORPUS[idx] if idx < len(CORPUS) else "?"
        lines.append(f"{idx:>3} | {text[:26]!r} -> {sorted(per_msg[idx])}")
    (OUT / f"hits_{tag}.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"tag={tag}: {len(per_msg)}/{len(CORPUS)} 条消息命中了触发器")
    print(f"明细 -> eval/manual/_ab_out/hits_{tag}.txt")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(1)
    mode, tag = sys.argv[1], sys.argv[2]
    if mode == "send":
        asyncio.run(send(tag))
    elif mode == "collect":
        collect(tag)
    else:
        print("unknown mode")
        raise SystemExit(1)
