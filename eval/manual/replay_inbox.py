"""收件箱调度器离线回放（确定性、零 LLM）：生产日志 → 真实 InboxScheduler。

读 ``data/logs/<日期>.log`` 里的 ``[收到事件]`` event_payload，按真实时间轴（加速）
喂给 ``InboxScheduler``。主 Agent 用「固定时长 + 回复第一个 pending 项」桩替代；
TurnRouter 用两种桩各跑一遍：soft 一律 dismissed（``dismiss``）/ 一律放行（``pass``）。

数据只在运行时读取，不复制进仓库，也不打印原文（AGENTS.md §1.13）。

已知限制：日志里没有 Bot 出站的平台 msg_id，引用 Bot（quote_self）无法还原；续聊窗口按
「被回复后 follow_up_window 秒」近似，不含 max_total / max_replies。

用法::
    uv run python eval/manual/replay_inbox.py --log <日志文件路径>
        --group <群号> --names <角色名> [--run-seconds 8] [--speed 50]
"""

from __future__ import annotations

import re
import time
import asyncio
import argparse
import statistics
from typing import Literal
from pathlib import Path
from datetime import datetime
from dataclasses import field, dataclass

from gsuid_core.bot import _Bot
from gsuid_core.models import Event, TaskContext
from gsuid_core.message_history import get_history_manager
from gsuid_core.ai_core.inbox.turn import InboxTurn
from gsuid_core.ai_core.inbox.state import WAKE_STRENGTH, WakeItem, WakeKind, PendingEntry, classify_wake
from gsuid_core.ai_core.inbox.scheduler import TurnOutcome, InboxScheduler
from gsuid_core.ai_core.configs.ai_config import ai_config

RouterStub = Literal["dismiss", "pass"]

_FIELD_RE = {
    "bot_id": re.compile(r"Event\(bot_id='([^']*)'"),
    "bot_self_id": re.compile(r"bot_self_id='([^']*)'"),
    "msg_id": re.compile(r"msg_id='([^']*)'"),
    "user_type": re.compile(r"user_type='([^']*)'"),
    "group_id": re.compile(r"group_id='([^']*)'"),
    "user_id": re.compile(r", user_id='([^']*)', sender="),
    "raw_text": re.compile(r"raw_text='((?:[^'\\]|\\.)*)'"),
    "reply_id": re.compile(r"reply_id='([^']*)'"),
    "ws_bot_id": re.compile(r"WS_BOT_ID='([^']*)'"),
}
_AT_BOT_RE = re.compile(r"at_bot=True")
_AT_LIST_RE = re.compile(r"at_list=\[([^\]]*)\]")
_TS_RE = re.compile(r'"timestamp": "(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})"')
_PAYLOAD_RE = re.compile(r'"event_payload": "(.*)", "event": "[^"]*\[收到事件\]')


@dataclass(frozen=True)
class Inbound:
    ts: float
    bot_id: str
    bot_self_id: str
    msg_id: str
    user_type: str
    group_id: str
    user_id: str
    raw_text: str
    reply_id: str
    at_bot: bool
    at_list: tuple[str, ...]
    ws_bot_id: str


@dataclass
class Stats:
    handled: dict[int, int] = field(default_factory=dict)
    wake_at: dict[int, float] = field(default_factory=dict)
    wake_kind: dict[int, WakeKind] = field(default_factory=dict)
    wake_user: dict[int, str] = field(default_factory=dict)
    first_turn_at: dict[int, float] = field(default_factory=dict)
    turns: int = 0
    dirty_arrivals: int = 0


def _field(name: str, payload: str) -> str:
    m = _FIELD_RE[name].search(payload)
    return m.group(1) if m else ""


def parse_log(path: Path, group_id: str) -> list[Inbound]:
    year = int(path.stem[:4])
    out: list[Inbound] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "[收到事件]" not in line:
            continue
        pm = _PAYLOAD_RE.search(line)
        tm = _TS_RE.search(line)
        if pm is None or tm is None:
            continue
        payload = pm.group(1)
        if _field("group_id", payload) != group_id:
            continue
        mon, day, hh, mi, ss = (int(x) for x in tm.groups())
        at_raw = _AT_LIST_RE.search(payload)
        at_list = tuple(x.strip(" '") for x in at_raw.group(1).split(",") if x.strip()) if at_raw else ()
        out.append(
            Inbound(
                ts=datetime(year, mon, day, hh, mi, ss).timestamp(),
                bot_id=_field("bot_id", payload),
                bot_self_id=_field("bot_self_id", payload),
                msg_id=_field("msg_id", payload),
                user_type=_field("user_type", payload) or "group",
                group_id=group_id,
                user_id=_field("user_id", payload),
                raw_text=_field("raw_text", payload),
                reply_id=_field("reply_id", payload),
                at_bot=_AT_BOT_RE.search(payload) is not None,
                at_list=at_list,
                ws_bot_id=_field("ws_bot_id", payload),
            )
        )
    out.sort(key=lambda x: x.ts)
    return out


class VirtualClock:
    """真实时间加速 speed 倍，scheduler 的 settle / TTL 同比缩放。"""

    def __init__(self, origin: float, speed: float) -> None:
        self.origin = origin
        self.speed = speed
        self.t0 = time.monotonic()
        self.offset = 0.0

    def now(self) -> float:
        return self.origin + self.offset + (time.monotonic() - self.t0) * self.speed

    def jump_to(self, ts: float) -> None:
        """调度器全空闲时直接跳过静默段，只在有活动时按 speed 真实流逝。"""
        gap = ts - self.now()
        if gap > 0:
            self.offset += gap

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds) / self.speed)

    async def sleep_until(self, ts: float) -> None:
        await self.sleep(ts - self.now())


async def _consume(bot: _Bot) -> None:
    while True:
        task_ctx = await bot.queue.get()
        if isinstance(task_ctx, TaskContext):
            asyncio.ensure_future(task_ctx.coro)


def _to_event(ib: Inbound) -> Event:
    return Event(
        bot_id=ib.bot_id,
        bot_self_id=ib.bot_self_id,
        msg_id=ib.msg_id,
        user_type="direct" if ib.user_type == "direct" else "group",
        group_id=ib.group_id,
        user_id=ib.user_id,
        WS_BOT_ID=ib.ws_bot_id or "replay",
        real_bot_id=ib.bot_id,
    )


def legacy_latencies(
    wakes: list[tuple[float, WakeKind, str]], run_s: float, debounce_s: float
) -> tuple[int, list[float]]:
    """旧链路近似：hard 逐条排队；soft 按同人 debounce 合并后排队；会话锁串行。

    等待从消息到达算起（含 debounce），与收件箱口径一致。
    """
    jobs: list[tuple[float, float]] = []
    open_soft: dict[str, int] = {}
    for ts, kind, uid in wakes:
        if WAKE_STRENGTH[kind] == "hard":
            jobs.append((ts, ts))
            continue
        idx = open_soft[uid] if uid in open_soft else -1
        if idx >= 0 and ts <= jobs[idx][0]:
            jobs[idx] = (ts + debounce_s, jobs[idx][1])
            continue
        open_soft[uid] = len(jobs)
        jobs.append((ts + debounce_s, ts))
    free_at = 0.0
    lat: list[float] = []
    for ready, arrival in sorted(jobs):
        begin = max(ready, free_at)
        lat.append(begin - arrival)
        free_at = begin + run_s
    return len(jobs), lat


async def replay(
    events: list[Inbound],
    *,
    names: list[str],
    run_s: float,
    speed: float,
    router: RouterStub,
) -> tuple[Stats, int, list[float]]:
    clock = VirtualClock(events[0].ts - 1.0, speed)
    stats = Stats()
    bot = _Bot("replay")
    consumer = asyncio.create_task(_consume(bot))
    fw = int(ai_config.get_config("follow_up_window").data)
    # 续聊窗口按虚拟时钟自管：followup_window 模块读墙钟，加速回放下会失真
    window_until: dict[str, float] = {}

    async def runner(turn: InboxTurn, _ws: _Bot) -> TurnOutcome:
        now = clock.now()
        for e in turn.entries:
            seq = e.item.seq
            stats.handled[seq] = (stats.handled[seq] if seq in stats.handled else 0) + 1
            if seq not in stats.first_turn_at:
                stats.first_turn_at[seq] = now
        has_hard = any(e.item.strength == "hard" for e in turn.entries)
        if not has_hard and router == "dismiss":
            return TurnOutcome(reached_model=False, error=False)
        stats.turns += 1
        await clock.sleep(run_s)
        first = turn.primary
        window_until[first.item.user_id] = clock.now() + fw
        return TurnOutcome(reached_model=True, error=False, answered=frozenset({first.item.seq}))

    sched = InboxScheduler(clock=clock.now, sleep=clock.sleep, runner=runner)
    history = get_history_manager()
    for ib in events:
        if all(inbox.phase == "idle" for inbox in sched._inboxes.values()):
            clock.jump_to(ib.ts)
        await clock.sleep_until(ib.ts)
        ev = _to_event(ib)
        meta: dict[str, object] = {"msg_id": ib.msg_id}
        if ib.reply_id:
            meta["reply_id"] = ib.reply_id
        if ib.at_bot:
            meta["at_bot"] = True
        if ib.at_list:
            meta["at_list"] = list(ib.at_list)
        rec = history.add_message(ev, role="user", content=ib.raw_text, metadata=meta)
        link = rec.link
        if link is None:
            continue
        kind = classify_wake(
            user_type=ev.user_type or "group",
            at_self=link.at_self,
            reply_to_author=link.reply_to_author,
            raw_text=ib.raw_text,
            wake_names=names,
            in_followup=ib.user_id in window_until and clock.now() <= window_until[ib.user_id] and not ib.at_list,
            has_at_others=bool(link.at_targets),
        )
        if kind is None:
            continue
        stats.wake_at[link.seq] = clock.now()
        stats.wake_kind[link.seq] = kind
        stats.wake_user[link.seq] = ib.user_id
        key = (ev.session_id, "replay")
        if key in sched._inboxes and sched._inboxes[key].phase == "running":
            stats.dirty_arrivals += 1
        item = WakeItem(link.seq, ib.user_id, kind, WAKE_STRENGTH[kind], clock.now())
        sched.submit(bot, PendingEntry(item=item, event=ev, persona_name="replay"))

    await clock.sleep(run_s * 4 + 10)
    leftover = sum(
        1 for inbox in sched._inboxes.values() for entry in inbox.items.values() if entry.item.state == "pending"
    )
    consumer.cancel()
    lat = [stats.first_turn_at[s] - stats.wake_at[s] for s in stats.wake_at if s in stats.first_turn_at]
    return stats, leftover, lat


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("--group", required=True)
    parser.add_argument("--names", required=True, help="人格名 / 唤醒词，逗号分隔")
    parser.add_argument("--run-seconds", type=float, default=8.0)
    parser.add_argument("--speed", type=float, default=10.0)
    args = parser.parse_args()
    events = parse_log(args.log, args.group)
    if not events:
        raise SystemExit("该群在日志里没有入站事件")
    from gsuid_core.ai_core.inbox.scheduler import _SOFT_SETTLE_S

    names = [n.strip() for n in args.names.split(",") if n.strip()]
    debounce = _SOFT_SETTLE_S
    print(f"入站 {len(events)} 条，跨度 {(events[-1].ts - events[0].ts) / 60:.1f} 分钟")
    for router in ("dismiss", "pass"):
        get_history_manager().clear_history(_to_event(events[0]))
        stats, leftover, lat = await replay(
            events, names=names, run_s=args.run_seconds, speed=args.speed, router=router
        )
        wakes = sorted(stats.wake_at.items())
        legacy_jobs, legacy_lat = legacy_latencies(
            [(ts, stats.wake_kind[s], stats.wake_user[s]) for s, ts in wakes], args.run_seconds, debounce
        )
        n = max(1, len(wakes))
        max_handled = max(stats.handled.values()) if stats.handled else 0
        print(f"\n--- router 桩 = {router} ---")
        print(f"唤醒项 {len(wakes)}（hard {sum(1 for s in stats.wake_kind.values() if WAKE_STRENGTH[s] == 'hard')}）")
        print(f"同一唤醒项被批次处理次数 max = {max_handled}（通过线 = 1）")
        print(f"主模型轮数 / 100 唤醒：收件箱 {stats.turns * 100 / n:.1f}，旧链路近似 {legacy_jobs * 100 / n:.1f}")
        print(f"运行中到达、并入下一轮的唤醒：{stats.dirty_arrivals}")
        print(f"回放结束 pending 残留 = {leftover}（通过线 = 0）")
        p50, p95 = _pct(lat, 0.5), _pct(lat, 0.95)
        l50, l95 = _pct(legacy_lat, 0.5), _pct(legacy_lat, 0.95)
        print(f"首响等待 P50/P95：收件箱 {p50:.1f}s/{p95:.1f}s，旧链路近似 {l50:.1f}s/{l95:.1f}s")
        print(f"增量 P50 {p50 - l50:+.1f}s（≤ +1.5s）P95 {p95 - l95:+.1f}s（≤ +5s）")
        if lat:
            print(f"平均 {statistics.mean(lat):.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
