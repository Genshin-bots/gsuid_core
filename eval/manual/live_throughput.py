"""端到端吞吐 A/B：向运行中的 core 灌 N 条消息，测进程 CPU 增量与墙钟。

匹配是纯 CPU，所以「每条消息的 CPU 增量」是最灵敏的指标——比墙钟稳得多
（墙钟会被 DB 写、AI 后台任务、GC 抖动淹没）。

同时打一份「消息构成」：90% 群聊噪声 + 10% 命令，接近真实群流量。

用法：
    uv run python eval/manual/live_throughput.py <tag> [count] [interval_ms]
    uv run python eval/manual/live_throughput.py cpu <tag>   # 读取采样结果
"""

from __future__ import annotations

import sys
import json
import time
import asyncio
from pathlib import Path

import psutil
import websockets
from msgspec import json as msgjson

URL = "ws://127.0.0.1:8765/ws/ThruBot"
OUT = Path(__file__).resolve().parents[2] / "eval" / "manual" / "_ab_out"
USER = "99000002"
GROUP = "99000002"
NOISE = [
    "今天天气真不错啊大家吃了吗",
    "哈哈哈哈哈哈哈",
    "我先去吃个饭",
    "这个版本什么时候更新",
    "有人一起打副本吗",
    "刚下班累死了",
    "晚安各位",
    "打卡第100天",
    "有没有推荐的番剧",
    "我手机快没电了",
]
COMMANDS = ["帮助", "原神帮助", "gs帮助", "help", "原神 抽卡 记录"]


def _payload(tag: str, i: int, text: str) -> dict:
    return {
        "bot_id": "ThruBot",
        "bot_self_id": "99999",
        "msg_id": f"th-{tag}-{i}",
        "user_type": "group",
        "group_id": GROUP,
        "user_id": USER,
        "sender": {"nickname": "thru", "user_id": int(USER)},
        "user_pm": 6,
        "content": [{"type": "text", "data": text}],
    }


def _mix(i: int) -> str:
    if i % 10 == 0:
        return COMMANDS[(i // 10) % len(COMMANDS)]
    return NOISE[i % len(NOISE)]


def _core_proc() -> psutil.Process:
    """找到跑 core 的进程（uv run core 会先起一个 python 再起子进程，取 CPU 最大的）。"""
    cands = [
        p for p in psutil.process_iter(["name", "cmdline"]) if p.info["name"] and "python" in p.info["name"].lower()
    ]
    best, best_cpu = None, -1.0
    for p in cands:
        cl = p.info["cmdline"] or []
        joined = " ".join(cl)
        if "core" not in joined and "gsuid" not in joined:
            continue
        try:
            c = p.cpu_times().user + p.cpu_times().system
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if c > best_cpu:
            best, best_cpu = p, c
    if best is None:
        raise SystemExit("找不到 core 进程")
    return best


def _count_drops(tag: str) -> int:
    """入站槽满会被丢，统计条数——否则"发了多少"不等于"处理了多少"。"""
    n = 0
    for f in sorted(Path("data/logs").glob("*.log"), key=lambda p: p.stat().st_mtime)[-1:]:
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            if f"th-{tag}-" in line and "inbound_busy" in line:
                n += 1
    return n


async def run(tag: str, count: int, interval_ms: int, port: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    proc = _core_proc()
    uri = f"ws://127.0.0.1:{port}/ws/ThruBot?token=1"
    async with websockets.connect(uri, max_size=None) as ws:
        # 先灌 20 条预热，避免把插件首次触发的懒加载算进来
        for i in range(20):
            await ws.send(msgjson.encode(_payload(f"{tag}warm", i, _mix(i))))
        await asyncio.sleep(3.0)

        # 空闲基线：core 有心跳/定时/TTL 等后台任务，不扣掉它就全是噪声
        c0 = proc.cpu_times()
        w0 = time.perf_counter()
        await asyncio.sleep(3.0)
        c1 = proc.cpu_times()
        idle_wall = time.perf_counter() - w0
        idle_cpu = (c1.user - c0.user) + (c1.system - c0.system)

        t0 = proc.cpu_times()
        wall0 = time.perf_counter()
        sent = 0
        for i in range(count):
            await ws.send(msgjson.encode(_payload(tag, i, _mix(i))))
            sent += 1
            if interval_ms:
                await asyncio.sleep(interval_ms / 1000)
        wall = time.perf_counter() - wall0
        t1 = proc.cpu_times()
        cpu = (t1.user - t0.user) + (t1.system - t0.system)
        await asyncio.sleep(2.0)
    drops = _count_drops(tag)

    idle_rate = idle_cpu / idle_wall  # 每秒空闲 CPU（秒/秒）
    busy_rate = cpu / wall
    net = (busy_rate - idle_rate) * wall  # 扣掉后台后的净消息 CPU
    result = {
        "tag": tag,
        "count": sent,
        "interval_ms": interval_ms,
        "drops": drops,
        "wall_s": round(wall, 3),
        "cpu_s": round(cpu, 3),
        "msg_per_s_wall": round(sent / wall, 1),
        "cpu_ms_per_msg_raw": round(cpu / sent * 1000, 3),
        "idle_cpu_cores": round(idle_rate, 3),
        "busy_cpu_cores": round(busy_rate, 3),
        "net_cpu_ms_per_msg": round(net / sent * 1000, 3),
    }
    (OUT / f"thru_{tag}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if sys.argv[1] == "cpu":
        print((OUT / f"thru_{sys.argv[2]}.json").read_text(encoding="utf-8"))
    else:
        asyncio.run(
            run(
                sys.argv[1],
                int(sys.argv[2]) if len(sys.argv) > 2 else 300,
                int(sys.argv[3]) if len(sys.argv) > 3 else 20,
                sys.argv[4] if len(sys.argv) > 4 else "8765",
            )
        )
