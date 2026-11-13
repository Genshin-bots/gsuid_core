"""会话收件箱调度器：settle → 组轮 → 单飞执行 → 落定。

同会话同一时刻只跑一轮；运行中到达的唤醒项只入 items 并标 dirty，
本轮结束后合并成下一轮。落定只发生一次。
"""

from __future__ import annotations

import time
import asyncio
from typing import TYPE_CHECKING, Callable, Awaitable
from collections import OrderedDict
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import TaskContext
from gsuid_core.ai_core.inbox.turn import InboxTurn, backfill_due_for

if TYPE_CHECKING:
    from gsuid_core.bot import _Bot
from gsuid_core.ai_core.inbox.state import (
    WAKE_TTL_SECONDS,
    SAME_SPEAKER_EXTEND_S,
    PendingEntry,
    SessionInbox,
)

TurnRunner = Callable[[InboxTurn, "_Bot"], Awaitable["TurnOutcome"]]

# runner 执行失败后退回 pending 的最多重试次数；超限记 expired
_MAX_RETRIES = 1
# 私聊 / 群聊 hard 唤醒与软唤醒静默窗（秒）
_PRIVATE_HARD_SETTLE_S = 0.8
_GROUP_HARD_SETTLE_S = 1.5
_SOFT_SETTLE_S = 5.0
_READ_SEQ_CAP = 1024


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    reached_model: bool
    error: bool
    answered: frozenset[int] = frozenset()


class InboxScheduler:
    """时钟 / sleep / runner 均为构造注入，单测用假实现驱动纯状态机。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        runner: TurnRunner,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._runner = runner
        self._inboxes: dict[tuple[str, str], SessionInbox] = {}
        self._settle_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._ws: dict[tuple[str, str], _Bot] = {}
        self._last_read_seq: OrderedDict[tuple[str, str], int] = OrderedDict()

    def submit(self, ws: _Bot, entry: PendingEntry) -> None:
        key = (entry.event.session_id, entry.persona_name)
        self._ws[key] = ws
        if key not in self._inboxes:
            self._inboxes[key] = SessionInbox(
                session_id=key[0],
                persona_name=key[1],
                read_seq=self._resolve_initial_read_seq(key, entry),
            )
        inbox = self._inboxes[key]
        inbox.items[entry.item.seq] = entry

        if inbox.phase == "running":
            inbox.dirty = True
            return

        now = self._clock()
        if inbox.phase == "idle":
            inbox.phase = "settling"
            inbox.settle_started = now
            inbox.settle_deadline = now + self._initial_settle_s(entry)
            inbox.last_speaker = entry.item.user_id
            self._spawn_settle(key)
            return

        # settling：同说话人续发延长（受总上限约束）；hard 项把窗收回起步值
        if entry.item.user_id == inbox.last_speaker:
            inbox.settle_deadline = min(
                max(inbox.settle_deadline, now) + SAME_SPEAKER_EXTEND_S,
                inbox.settle_started + self._settle_max_s(),
            )
        if entry.item.strength == "hard":
            inbox.settle_deadline = min(inbox.settle_deadline, now + self._initial_settle_s(entry))
        inbox.last_speaker = entry.item.user_id

    @staticmethod
    def _initial_read_seq(entry: PendingEntry) -> int:
        """从唤醒消息本身起读：首轮 delta 即含该条，B 轨能看到触发消息。"""
        return max(0, entry.item.seq - 1)

    def _resolve_initial_read_seq(self, key: tuple[str, str], entry: PendingEntry) -> int:
        """跨 _drop 延续已读游标；序号回绕（如清史后重启计数）时退回 seq - 1。"""
        if key in self._last_read_seq:
            prev = self._last_read_seq[key]
            if prev < entry.item.seq:
                self._last_read_seq.move_to_end(key)
                return prev
            self._last_read_seq.pop(key, None)
        return self._initial_read_seq(entry)

    def _remember_read_seq(self, key: tuple[str, str], seq: int) -> None:
        self._last_read_seq[key] = seq
        self._last_read_seq.move_to_end(key)
        while len(self._last_read_seq) > _READ_SEQ_CAP:
            self._last_read_seq.popitem(last=False)

    def _settle_max_s(self) -> float:
        from gsuid_core.ai_core.configs.ai_config import ai_config

        return float(ai_config.get_config("inbox_settle_max_ms").data) / 1000.0

    def _initial_settle_s(self, entry: PendingEntry) -> float:
        if entry.item.strength == "hard":
            if entry.event.user_type == "direct":
                return _PRIVATE_HARD_SETTLE_S
            return _GROUP_HARD_SETTLE_S
        return _SOFT_SETTLE_S

    def _resume_settle_s(self, inbox: SessionInbox) -> float:
        for entry in inbox.items.values():
            if entry.item.state == "pending" and entry.item.strength == "hard":
                return self._initial_settle_s(entry)
        return _SOFT_SETTLE_S

    def _drop(self, key: tuple[str, str]) -> None:
        """会话清空后的三张表一起收，_ws 里的 _Bot 引用不留着常驻。"""
        self._inboxes.pop(key, None)
        self._ws.pop(key, None)
        self._settle_tasks.pop(key, None)

    def _spawn_settle(self, key: tuple[str, str]) -> None:
        if key in self._settle_tasks and not self._settle_tasks[key].done():
            return
        self._settle_tasks[key] = asyncio.create_task(self._settle_then_run(key))

    async def _settle_then_run(self, key: tuple[str, str]) -> None:
        while True:
            if key not in self._inboxes:
                return
            inbox = self._inboxes[key]
            if inbox.phase != "settling":
                return
            wait = inbox.settle_deadline - self._clock()
            if wait <= 0:
                break
            await self._sleep(wait)
        if key not in self._inboxes:
            return
        inbox = self._inboxes[key]
        if inbox.phase != "settling":
            return
        turn = self._build_turn(inbox)
        if turn is None:
            self._drop(key)
            return
        inbox.phase = "running"
        inbox.dirty = False
        if key not in self._ws:
            inbox.phase = "idle"
            return
        ws = self._ws[key]
        coro = self._run_turn(key, turn, ws)
        priority = min((e.event.user_pm for e in turn.entries), default=2)
        ws.queue.put_nowait(TaskContext(coro=coro, name="handle_inbox_turn", priority=priority))

    def _build_turn(self, inbox: SessionInbox) -> InboxTurn | None:
        now = self._clock()
        for entry in inbox.items.values():
            item = entry.item
            if item.state == "pending" and now - item.created_at > WAKE_TTL_SECONDS[item.kind]:
                item.state = "expired"
                logger.info(t("log.inbox.item_expired", seq=item.seq, kind=item.kind, reason="ttl"))
        pending = [e for e in inbox.items.values() if e.item.state == "pending"]
        if not pending:
            return None
        entries = tuple(sorted(pending, key=lambda e: (e.item.strength != "hard", e.item.seq)))
        from gsuid_core.message_history import get_history_manager

        return InboxTurn(
            session_id=inbox.session_id,
            persona_name=inbox.persona_name,
            entries=entries,
            from_seq=inbox.read_seq,
            head_seq=get_history_manager().head_seq(entries[0].event),
            backfill=backfill_due_for(inbox.session_id),
        )

    async def _run_turn(self, key: tuple[str, str], turn: InboxTurn, ws: _Bot) -> None:
        # runner（handle_inbox_turn）自身保证不抛：异常在边界转成 TurnOutcome(error=True)
        outcome = await self._runner(turn, ws)
        self._finish(key, turn, outcome)

    def _finish(self, key: tuple[str, str], turn: InboxTurn, outcome: TurnOutcome) -> None:
        if key not in self._inboxes:
            return
        inbox = self._inboxes[key]
        batch = {e.item.seq: e for e in turn.entries}
        if outcome.error:
            for seq in batch:
                if seq not in inbox.items or inbox.items[seq].item.state != "pending":
                    continue
                entry = inbox.items[seq]
                entry.item.retries += 1
                if entry.item.retries > _MAX_RETRIES:
                    entry.item.state = "expired"
                    logger.warning(t("log.inbox.item_expired", seq=seq, kind=entry.item.kind, reason="retry"))
        elif not outcome.reached_model:
            # 早退批次没进 B 轨：read_seq 不推进，下一轮增量仍能读到这些消息
            for seq in batch:
                if seq in inbox.items and inbox.items[seq].item.state == "pending":
                    inbox.items[seq].item.state = "dismissed"
        else:
            for seq in batch:
                if seq not in inbox.items or inbox.items[seq].item.state != "pending":
                    continue
                entry = inbox.items[seq]
                entry.item.state = "answered" if seq in outcome.answered else "dismissed"
            inbox.read_seq = turn.head_seq
            self._remember_read_seq(key, turn.head_seq)

        settled = [s for s, e in inbox.items.items() if e.item.state != "pending"]
        for seq in settled:
            del inbox.items[seq]
        if any(e.item.state == "pending" for e in inbox.items.values()) or inbox.dirty:
            inbox.phase = "settling"
            inbox.settle_started = self._clock()
            inbox.settle_deadline = inbox.settle_started + self._resume_settle_s(inbox)
            self._spawn_settle(key)
        else:
            self._drop(key)


_scheduler: InboxScheduler | None = None
_turn_runner: TurnRunner | None = None


def set_turn_runner(runner: TurnRunner) -> None:
    """生产在启动时注入 handle_ai.handle_inbox_turn；测试注入桩。"""
    global _turn_runner
    _turn_runner = runner


async def _dispatch_turn(turn: InboxTurn, ws: _Bot) -> TurnOutcome:
    if _turn_runner is None:
        return TurnOutcome(reached_model=False, error=True, answered=frozenset())
    return await _turn_runner(turn, ws)


def get_inbox_scheduler() -> InboxScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = InboxScheduler(clock=time.time, sleep=asyncio.sleep, runner=_dispatch_turn)
    return _scheduler
