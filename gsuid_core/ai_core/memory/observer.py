"""观察者管道（schema_v2）。

只做协议级清洗（命令回显 / 用户命令 / 注入 / 凭据 / 复读）与富元数据保留；
不再做语义价值分级。所有放行消息进入每个 scope 的环形窗口，由 Pass 1 统一分诊。
"""

import re
import time
import queue as sync_queue
from typing import TYPE_CHECKING, Optional, Sequence
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger

if TYPE_CHECKING:
    from gsuid_core.ai_core.memory.retrieval.types import Episode

# 评测侧时间戳格式表：ISO8601 / Unix / LongMemEval / BEAM，全部失败兜底 None。
_TIMESTAMP_STRPTIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%B-%d-%Y",
    "%b-%d-%Y",
    "%d-%B-%Y",
    "%d-%b-%Y",
)
_LONGMEMEVAL_WEEKDAY_RE = re.compile(r"\s*\([A-Za-z]{3,9}\)\s*")


def parse_iso_or_unix_timestamp(raw: int | float | str | None) -> Optional[datetime]:
    """ISO8601 / Unix / 评测时间戳 → datetime（强制 UTC）。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(raw, str) and raw:
        s = raw.strip()
        if not s:
            return None
        s_iso = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s_iso)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
        s_clean = _LONGMEMEVAL_WEEKDAY_RE.sub(" ", s).strip()
        for fmt in _TIMESTAMP_STRPTIME_FORMATS:
            try:
                dt = datetime.strptime(s_clean, fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None
    return None


_observation_queue: sync_queue.Queue = sync_queue.Queue(maxsize=10_000)

_ASSISTANT_PREFIX = "__assistant_"

_REPEAT_WINDOW = 12
_recent_contents: dict[str, deque] = {}

_COMMAND_ECHO_RE = re.compile(
    r"^[🔨❌✅⚠️🚧\s]*[❌✅]?\s*.{0,20}(请输入正确|功能名称|不存在该功能|无效的指令|未找到命令)"
)
_INJECTION_RE = re.compile(
    r"(忘记(掉)?(所有|之前|你的)?(的)?(指令|设定|规则|对话|身份)"
    r"|ignore\s+(all\s+|previous\s+|the\s+)?(instructions|prompts?)"
    r"|你现在(开始)?(是|要扮演|将)"
    r"|重置(你的)?(指令|设定|人格)"
    r"|disregard\s+(all|previous))",
    re.IGNORECASE,
)
_USER_COMMAND_RE = re.compile(r"^[/#!！·、.]{1,2}\S")

_MEME_EXPLAIN_RE = re.compile(r"(是.{0,16}梗|的梗|梗的意思|.{0,12}是个梗|这是.{0,16}(梗|黑话))")
_MEME_LEARN_RE = re.compile(
    r"[「『\"“]([^」』\"”]{2,20})[」』\"”]\s*(?:是|就是)(.+?)(?:的梗|梗)"
    r"|([^」』\"”\s]{2,12})是(.+?)的梗"
)


def detect_meme_explain_intent(content: str) -> bool:
    return bool(_MEME_EXPLAIN_RE.search(content or ""))


def parse_meme_explain(content: str) -> tuple[str, str] | None:
    m = _MEME_LEARN_RE.search(content or "")
    if m is None:
        return None
    term = (m.group(1) or m.group(3) or "").strip()
    meaning = (m.group(2) or m.group(4) or "").strip()[:300]
    if len(term) < 2 or len(meaning) < 2:
        return None
    return term, meaning


def _schedule_meme_knowledge_write(
    bot_id: str,
    group_id: str | None,
    term: str,
    meaning: str,
    persona_name: str = "",
) -> None:
    if not bot_id:
        return
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _write() -> None:
        from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key, self_scope_key
        from gsuid_core.ai_core.cognition.types import CogKind
        from gsuid_core.ai_core.cognition.remember import MemoryWrite, remember
        from gsuid_core.ai_core.meme.database_model import AiMemeKnowledge

        scope_key = make_scope_key(ScopeType.GROUP, group_id) if group_id else ""
        row = await AiMemeKnowledge.upsert_term(
            bot_id=bot_id or "default",
            term=term,
            meaning=meaning,
            origin="",
            usage="",
            scope_key=scope_key,
            confidence=0.9,
            source="群友解释",
        )
        await remember(
            MemoryWrite(
                kind=CogKind.MEME_KNOWLEDGE,
                ref=str(row.id),
                scope_key=scope_key or self_scope_key(bot_id, persona_name),
                owner_user_id="",
                title=term,
                summary=meaning[:120],
                source="群友解释",
            )
        )

    loop.create_task(_write())


@dataclass
class ObservationRecord:
    """Observer Pipeline 的最小数据单元。"""

    raw_content: str
    speaker_id: str
    group_id: Optional[str]
    scope_key: str
    timestamp: datetime
    message_type: str
    value_tier: str = "HIGH"
    is_correction: bool = False
    message_id: Optional[str] = None
    speaker_name: str = ""
    reply_to_msg_id: str = ""
    reply_preview: str = ""
    at_targets: tuple[str, ...] = ()
    bot_id: str = ""
    persona_name: str = ""
    command_hit: bool = False


class _ScopeWindow:
    """单 scope 环形窗口：容量 / 静默 / 最老封顶三出口。"""

    __slots__ = ("records", "last_activity", "first_at")

    def __init__(self) -> None:
        self.records: deque[ObservationRecord] = deque()
        self.last_activity: float = time.time()
        self.first_at: float = self.last_activity

    def push(self, rec: ObservationRecord) -> None:
        if not self.records:
            self.first_at = time.time()
        self.records.append(rec)
        self.last_activity = time.time()

    def snapshot(self) -> list[ObservationRecord]:
        return list(self.records)

    def should_flush(
        self,
        now: float,
        *,
        capacity: int,
        idle_seconds: int,
        max_age_seconds: int,
        force: bool = False,
    ) -> bool:
        if not self.records:
            return False
        if force:
            return True
        if len(self.records) >= capacity:
            return True
        if idle_seconds > 0 and len(self.records) >= 4 and (now - self.last_activity) >= idle_seconds:
            return True
        return (now - self.first_at) >= max_age_seconds

    def drain(self) -> list[ObservationRecord]:
        records = list(self.records)
        self.records.clear()
        return records


def _is_repeat(scope_key: str, content: str) -> bool:
    window = _recent_contents.get(scope_key)
    if window is None:
        window = deque(maxlen=_REPEAT_WINDOW)
        _recent_contents[scope_key] = window
    if content in window:
        return True
    window.append(content)
    return False


def _looks_like_command(text: str) -> bool:
    from gsuid_core.config import core_config

    starts = [s for s in (core_config.get_config("command_start") or []) if s]
    if starts and any(text.startswith(s) for s in starts):
        return True
    return bool(_USER_COMMAND_RE.match(text)) and len(text) <= 30


def _gate(
    content: str,
    speaker_id: str,
    bot_self_id: str,
    observer_blacklist: list[str],
    group_id: Optional[str],
    scope_key: str,
) -> Optional[str]:
    """协议级清洗。返回 "HIGH" 或 None。"""
    if speaker_id == bot_self_id:
        return None
    if group_id and group_id in observer_blacklist:
        return None
    stripped = content.strip()
    if not stripped:
        return None
    if stripped.startswith("[图片]") and len(stripped) < 10:
        return None
    if _COMMAND_ECHO_RE.search(stripped):
        logger.trace(t("log.memory.observer_command_echo_filter", p0=stripped[:30]))
        return None
    if _looks_like_command(stripped):
        logger.trace(t("log.memory.observer_user_command_typo", p0=stripped[:30]))
        return None
    if _INJECTION_RE.search(stripped):
        logger.trace(t("log.memory.observer_injection_signature_filter_inject", p0=stripped[:30]))
        return None
    from gsuid_core.ai_core.mcp.pii import has_credential_form, is_credential_command

    if is_credential_command(stripped) or has_credential_form(stripped):
        logger.trace(t("log.memory.observer_credential_filtered"))
        return None
    if _is_repeat(scope_key, stripped):
        logger.trace(t("log.memory.observer_repetition_filter_matched", p0=stripped[:30]))
        return None
    return "HIGH"


async def observe(
    content: str,
    speaker_id: str,
    group_id: Optional[str],
    bot_self_id: str,
    observer_blacklist: list[str],
    message_type: str = "group_msg",
    timestamp: Optional[datetime] = None,
    *,
    force_scope_key: Optional[str] = None,
    bot_id: str = "",
    persona_name: str = "",
    message_id: Optional[str] = None,
    speaker_name: str = "",
    reply_to_msg_id: str = "",
    reply_preview: str = "",
    at_targets: Sequence[str] = (),
) -> None:
    """向观察队列投递一条消息记录（不 await 调用为主）。"""
    from .scope import ScopeType, make_scope_key, self_scope_key

    if message_id:
        from gsuid_core.message_history import get_history_manager

        if get_history_manager().is_command_hit(message_id):
            return

    is_self_speech = force_scope_key is None and speaker_id.startswith(_ASSISTANT_PREFIX)
    if is_self_speech:
        stripped = content.strip()
        if not stripped:
            return
        scope_key = self_scope_key(bot_self_id, persona_name)
        value_tier = "LOW"
    else:
        if force_scope_key is not None:
            scope_key = force_scope_key
        else:
            scope_key = make_scope_key(
                ScopeType.GROUP if group_id else ScopeType.USER_GLOBAL,
                group_id if group_id else speaker_id,
            )
        tier = _gate(content, speaker_id, bot_self_id, observer_blacklist, group_id, scope_key)
        if tier is None:
            return
        value_tier = tier
        if group_id:
            from gsuid_core.ai_core.mcp.pii import mask_pii

            content = mask_pii(content)
        if detect_meme_explain_intent(content):
            parsed = parse_meme_explain(content)
            if parsed is not None:
                _schedule_meme_knowledge_write(bot_id, group_id, parsed[0], parsed[1], persona_name)

    record = ObservationRecord(
        raw_content=content,
        speaker_id=speaker_id,
        group_id=group_id,
        scope_key=scope_key,
        timestamp=timestamp if timestamp is not None else datetime.now(timezone.utc),
        message_type=message_type,
        value_tier=value_tier,
        message_id=message_id,
        speaker_name=speaker_name or speaker_id,
        reply_to_msg_id=reply_to_msg_id,
        reply_preview=reply_preview[:120],
        at_targets=tuple(at_targets),
        bot_id=bot_id,
        persona_name=persona_name,
    )

    try:
        _observation_queue.put_nowait(record)
        try:
            from gsuid_core.ai_core.statistics import statistics_manager

            statistics_manager.record_memory_observation()
        except (ImportError, AttributeError):
            pass
    except sync_queue.Full:
        try:
            _observation_queue.get_nowait()
            _observation_queue.put_nowait(record)
        except sync_queue.Empty:
            logger.warning(t("log.memory.observation_queue_race"))


def get_observation_queue() -> sync_queue.Queue:
    return _observation_queue


def snapshot_pending_records(scope_keys: Sequence[str]) -> list[ObservationRecord]:
    """读取队列 + worker 环形窗口中尚未刷盘的记录（实时可见性）。"""
    wanted = frozenset(scope_keys)
    if not wanted:
        return []
    found: list[ObservationRecord] = []
    seen: set[tuple[str, str, datetime, str]] = set()

    def _take(rec: object) -> None:
        if not isinstance(rec, ObservationRecord) or rec.scope_key not in wanted:
            return
        key = (rec.scope_key, rec.speaker_id, rec.timestamp, rec.raw_content)
        if key in seen:
            return
        seen.add(key)
        found.append(rec)

    with _observation_queue.mutex:
        for rec in _observation_queue.queue:
            _take(rec)
    from gsuid_core.ai_core.memory.startup import peek_ingestion_buffers

    for rec in peek_ingestion_buffers(wanted):
        _take(rec)
    found.sort(key=lambda r: r.timestamp)
    return found


def format_pending_line(rec: ObservationRecord) -> str:
    """把未刷盘记录还原成与 Pass 1 输入一致的富结构行。"""
    name = rec.speaker_name or rec.speaker_id
    head = f"{name}({rec.speaker_id})"
    if rec.reply_to_msg_id:
        head += f" -> 回复{rec.reply_to_msg_id}"
        if rec.reply_preview:
            head += f": {rec.reply_preview}"
    if rec.at_targets:
        head += f" @{'、'.join(rec.at_targets)}"
    return f"{head}: {rec.raw_content}"


def pending_episodes_for_scopes(scope_keys: Sequence[str], *, cap: int = 8) -> list["Episode"]:
    """未落盘消息转成内存 Episode，供检索零延迟合并。"""
    from gsuid_core.ai_core.memory.retrieval.types import Episode

    records = snapshot_pending_records(scope_keys)
    if not records:
        return []
    chosen = records[-cap:]
    episodes: list[Episode] = []
    for rec in chosen:
        content = format_pending_line(rec)
        if not content.strip():
            continue
        ts = rec.timestamp
        valid_at = ts.isoformat() if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc).isoformat()
        eid = f"pending:{rec.scope_key}:{int(ts.timestamp() * 1000)}:{len(content)}"
        episodes.append(
            {
                "id": eid,
                "content": content,
                "valid_at": valid_at,
                "scope_key": rec.scope_key,
                "embedding": [],
            }
        )
    return episodes
