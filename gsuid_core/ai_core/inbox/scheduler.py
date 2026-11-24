"""会话收件箱调度器：settle → 组轮 → 单飞执行 → 落定。

同会话同一时刻只跑一轮；运行中到达的唤醒项只入 items 并标 dirty，
本轮结束后合并成下一轮。落定只发生一次。
"""

from __future__ import annotations

import re
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
# 私聊 / 群聊 hard 唤醒、未说完起手式与软唤醒静默窗（秒）
_PRIVATE_HARD_SETTLE_S = 0.8
_GROUP_HARD_SETTLE_S = 1.5
_INCOMPLETE_OPENER_SETTLE_S = 4.0
_SOFT_SETTLE_S = 5.0
_INBOX_SETTLE_MAX_S = 5.0
_READ_SEQ_CAP = 1024
_EXACT_DEDUP_TTL_S = 90.0
_EXACT_DEDUP_PER_SESSION_CAP = 16


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    reached_model: bool
    error: bool
    answered: frozenset[int] = frozenset()


_ExactDedupKey = tuple[str, str, bool, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _SettledExact:
    settled_at: float
    user_ids: frozenset[str]
    speaker_bound: bool


def _has_own_media(entry: PendingEntry) -> bool:
    ev = entry.event
    own_img = (
        any(i not in ev.reply_image_id_list for i in ev.image_id_list)
        or bool(ev.image_id and ev.image_id not in ev.reply_image_id_list)
        or bool(ev.image and not ev.image_id_list and not ev.reply_image_id_list)
    )
    return bool(own_img or ev.audio_id or ev.audio_id_list or ev.video_id or ev.video_id_list or ev.file or ev.node)


_OPENER_FILLERS = " \t，,。.!！~～啊呀呢哦吧诶喂"


def _is_incomplete_opener(entry: PendingEntry) -> bool:
    """群聊未说完起手式（纯 @Bot 或纯呼名/别名且无实质正文/问号/媒体/引用）：拉长首窗。"""
    ev = entry.event
    if ev.user_type == "direct" or entry.receipt is not None or entry.item.kind not in ("mention", "name"):
        return False
    if _has_own_media(entry) or ev.reply_id or ev.reply:
        return False
    text = (ev.raw_text or "").strip()
    if "?" in text or "？" in text:
        return False
    if entry.item.kind == "mention":
        if not text:
            return bool(ev.at_bot)
        return not text.strip(_OPENER_FILLERS)
    rem = text
    surfaces: list[str] = []
    if entry.persona_name:
        from gsuid_core.ai_core.memory.group_profile import collect_persona_surfaces

        surfaces.append(entry.persona_name)
        surfaces.extend(collect_persona_surfaces(entry.persona_name))
    for name in sorted({s.strip() for s in surfaces if s and s.strip()}, key=len, reverse=True):
        if name.isascii():
            rem = re.sub(re.escape(name), "", rem, flags=re.IGNORECASE)
        elif name in rem:
            rem = rem.replace(name, "")
    return not rem.strip(_OPENER_FILLERS)


def _exact_dedup_key(entry: PendingEntry) -> _ExactDedupKey | None:
    """群聊无自有附件消息的字节幂等键 (正文, reply_id, at_bot, at_list)；带媒体或空正文返回 None。"""
    ev = entry.event
    if ev.user_type == "direct" or entry.receipt is not None:
        return None
    if _has_own_media(entry):
        return None
    text = (ev.raw_text or "").strip()
    if not text:
        return None
    at_targets = tuple(str(x) for x in ev.at_list if x)
    return (text, str(ev.reply_id or ""), bool(ev.at_bot), at_targets)


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
        self._recent_exact: OrderedDict[tuple[str, str], OrderedDict[_ExactDedupKey, _SettledExact]] = OrderedDict()

    def has_active_session(self, session_id: str) -> bool:
        """指定会话是否有待收敛或正在执行的收件箱轮次。"""
        return any(
            sid == session_id and (inbox.phase != "idle" or bool(inbox.items))
            for (sid, _persona), inbox in self._inboxes.items()
        )

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

        # settling：仅同说话人续发时顺延（不跨人合批）；软窗首次升 hard 收回起步值
        had_hard = any(
            s != entry.item.seq and e.item.state == "pending" and e.item.strength == "hard"
            for s, e in inbox.items.items()
        )
        if entry.item.user_id == inbox.last_speaker:
            inbox.settle_deadline = min(
                max(inbox.settle_deadline, now) + SAME_SPEAKER_EXTEND_S,
                inbox.settle_started + self._settle_max_s(),
            )
        elif entry.item.strength == "hard" and not had_hard:
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

    def _prune_exact(self, key: tuple[str, str], now: float) -> OrderedDict[_ExactDedupKey, _SettledExact] | None:
        if key not in self._recent_exact:
            return None
        bucket = self._recent_exact[key]
        stale = [dk for dk, rec in bucket.items() if now - rec.settled_at > _EXACT_DEDUP_TTL_S]
        for dk in stale:
            del bucket[dk]
        if not bucket:
            del self._recent_exact[key]
            return None
        return bucket

    def _remember_exact(
        self,
        key: tuple[str, str],
        turn: InboxTurn,
        answered: frozenset[int],
        now: float,
    ) -> None:
        speaker_bound = bool(turn.observations) or turn.memory_dependent
        for entry in turn.entries:
            if entry.item.seq not in answered:
                continue
            dkey = _exact_dedup_key(entry)
            if dkey is None:
                continue
            if key not in self._recent_exact:
                self._recent_exact[key] = OrderedDict()
            bucket = self._recent_exact[key]
            prior_uids = bucket[dkey].user_ids if dkey in bucket else frozenset()
            prior_bound = bucket[dkey].speaker_bound if dkey in bucket else False
            bucket[dkey] = _SettledExact(
                settled_at=now,
                user_ids=prior_uids | {entry.item.user_id},
                speaker_bound=speaker_bound or prior_bound,
            )
            bucket.move_to_end(dkey)
            while len(bucket) > _EXACT_DEDUP_PER_SESSION_CAP:
                bucket.popitem(last=False)
            self._recent_exact.move_to_end(key)
            while len(self._recent_exact) > _READ_SEQ_CAP:
                self._recent_exact.popitem(last=False)

    def _settle_max_s(self) -> float:
        return _INBOX_SETTLE_MAX_S

    def _initial_settle_s(self, entry: PendingEntry) -> float:
        if entry.item.strength == "hard":
            if entry.event.user_type == "direct":
                return _PRIVATE_HARD_SETTLE_S
            if _is_incomplete_opener(entry):
                return min(_INCOMPLETE_OPENER_SETTLE_S, self._settle_max_s())
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

    def reset_session(self, session_id: str) -> None:
        """会话历史清空时同步重置已读游标与挂起项，防 seq 回绕错位。"""
        for key in [k for k in self._last_read_seq if k[0] == session_id]:
            self._last_read_seq.pop(key, None)
        for key in [k for k in self._recent_exact if k[0] == session_id]:
            del self._recent_exact[key]
        for key in [k for k in self._inboxes if k[0] == session_id]:
            inbox = self._inboxes[key]
            inbox.read_seq = 0
            inbox.items.clear()
            inbox.dirty = False
            if inbox.phase != "running":
                task = self._settle_tasks.pop(key, None)
                if task is not None and not task.done():
                    task.cancel()
                self._drop(key)

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
        key = (inbox.session_id, inbox.persona_name)
        settled_exact = self._prune_exact(key, now)
        for entry in inbox.items.values():
            item = entry.item
            if item.state != "pending":
                continue
            if now - item.created_at > WAKE_TTL_SECONDS[item.kind]:
                item.state = "expired"
                logger.info(t("log.inbox.item_expired", seq=item.seq, kind=item.kind, reason="ttl"))
                continue
            if settled_exact is not None:
                dkey = _exact_dedup_key(entry)
                if dkey is not None and dkey in settled_exact:
                    prev = settled_exact[dkey]
                    if not prev.speaker_bound or item.user_id in prev.user_ids:
                        item.state = "dismissed"
                        logger.info(t("log.inbox.item_dedup", seq=item.seq, reason="settled"))
        pending = [e for e in inbox.items.values() if e.item.state == "pending"]
        if not pending:
            return None
        ordered = sorted(pending, key=lambda e: (e.item.strength != "hard", e.item.seq))
        # 单轮只合同一说话人的连发；其他说话人的 pending 留待下一轮独立处理
        target_uid = ordered[0].item.user_id
        same_speaker = [e for e in ordered if e.item.user_id == target_uid]
        kept: list[PendingEntry] = []
        seen_batch: set[_ExactDedupKey] = set()
        for entry in same_speaker:
            dkey = _exact_dedup_key(entry)
            if dkey is not None:
                if dkey in seen_batch:
                    entry.item.state = "dismissed"
                    logger.info(t("log.inbox.item_dedup", seq=entry.item.seq, reason="batch"))
                    continue
                seen_batch.add(dkey)
            kept.append(entry)
        entries = tuple(kept)
        from gsuid_core.message_history import get_history_manager

        head_seq = get_history_manager().head_seq(entries[0].event)
        from_seq = inbox.read_seq if inbox.read_seq < head_seq else self._initial_read_seq(entries[0])
        return InboxTurn(
            session_id=inbox.session_id,
            persona_name=inbox.persona_name,
            entries=entries,
            from_seq=from_seq,
            head_seq=head_seq,
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
                if outcome.reached_model:
                    # 模型层已走完内层重试与主备换路，不再整批重跑避免放大卡死时长
                    entry.item.state = "expired"
                    logger.warning(t("log.inbox.item_expired", seq=seq, kind=entry.item.kind, reason="model_error"))
                    continue
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
            self._remember_exact(key, turn, outcome.answered, self._clock())

        settled = [s for s, e in inbox.items.items() if e.item.state != "pending"]
        for seq in settled:
            del inbox.items[seq]
        remaining = [e for e in inbox.items.values() if e.item.state == "pending"]
        if remaining or inbox.dirty:
            inbox.phase = "settling"
            inbox.settle_started = self._clock()
            inbox.settle_deadline = inbox.settle_started + self._resume_settle_s(inbox)
            if remaining:
                first_next = min(remaining, key=lambda e: (e.item.strength != "hard", e.item.seq))
                inbox.last_speaker = first_next.item.user_id
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
