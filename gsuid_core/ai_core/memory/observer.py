"""观察者管道（schema_v2）。

只做协议级清洗（命令回显 / 用户命令 / 注入 / 凭据 / 复读）与富元数据保留；
不再做语义价值分级。所有放行消息进入每个 scope 的环形窗口，由 Pass 1 统一分诊。
"""

import re
import queue as sync_queue
from typing import TYPE_CHECKING, Optional, Sequence
from datetime import datetime, timezone
from collections import deque
from dataclasses import dataclass

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.persona_reset.guard import is_scope_frozen

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
_PAREN_WEEKDAY_RE = re.compile(r"\s*\([A-Za-z]{3,9}\)\s*")


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
        s_clean = _PAREN_WEEKDAY_RE.sub(" ", s).strip()
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
# 仅拦截针对系统指令/人设/提示词的越狱注入，放行用户撤回自身历史计划或偏好的正常表达。
_INJECTION_RE = re.compile(
    r"(忘记(掉)?(所有|全部|一切|之前|你的|系统|初始|原有)?(的)?(系统|初始|安全|核心)?(指令|设定|人设|人格|规则|提示词|身份)"
    r"|ignore\s+(all\s+|previous\s+|the\s+|system\s+)?(instructions|prompts?|rules)"
    r"|你现在(开始)?(不再是|必须扮演|要扮演|切换为|改扮)"
    r"|重置(你的|全部|系统)?(指令|设定|人格|人设|提示词)"
    r"|disregard\s+(all|previous|system)\s+(instructions|prompts?|rules)?)",
    re.IGNORECASE,
)
_USER_COMMAND_RE = re.compile(r"^[/#!！·、.]{1,2}\S")


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
    reply_to_bot: bool = False
    message_id: Optional[str] = None
    speaker_name: str = ""
    reply_to_msg_id: str = ""
    reply_preview: str = ""
    at_targets: tuple[str, ...] = ()
    bot_id: str = ""
    persona_name: str = ""
    personal_credential: bool = False  # 出站依据含个人凭据：禁进群级事实抽取（§4.7）
    is_assistant_context: bool = False  # 本实例普通对话回复：仅作上下文参照，禁作事实源
    command_hit: bool = False
    defer_count: int = 0


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


_KNOWN_BOT_SELF_IDS: set[str] = set()


def _is_known_bot_speaker(speaker_id: str, bot_self_id: str) -> bool:
    sid = (speaker_id or "").strip()
    if not sid:
        return False
    if sid == (bot_self_id or "").strip():
        return True
    if sid.startswith(_ASSISTANT_PREFIX) or sid in _KNOWN_BOT_SELF_IDS:
        return True
    from gsuid_core.global_val import bot_val

    return any(sid in bmap for bmap in bot_val.values())


def _is_system_or_error_echo(stripped: str) -> bool:
    from gsuid_core.ai_core.control.lines import (
        INNER_OS_PREFIX,
        OOC_JUDGE_MARKER,
        FUND_CLAIM_PREFIX,
        SYSTEM_ECHO_PREFIXES,
        TOOL_NAME_LEAK_PREFIX,
    )
    from gsuid_core.ai_core.persona.settings import (
        DEFAULT_ERROR_QUOTA,
        DEFAULT_ERROR_GENERIC,
        DEFAULT_ERROR_TIMEOUT,
        DEFAULT_ERROR_CONTENT_POLICY,
    )

    if stripped.startswith(("[会话摘要] ",)):
        return False
    err_texts = (
        DEFAULT_ERROR_GENERIC,
        DEFAULT_ERROR_TIMEOUT,
        DEFAULT_ERROR_QUOTA,
        DEFAULT_ERROR_CONTENT_POLICY,
    )
    if any(e and (stripped.startswith(e) or e in stripped[:60]) for e in err_texts):
        return True
    if stripped.startswith(SYSTEM_ECHO_PREFIXES):
        return True
    return stripped.startswith(
        (FUND_CLAIM_PREFIX[:10], OOC_JUDGE_MARKER[:12], TOOL_NAME_LEAK_PREFIX[:12], INNER_OS_PREFIX[:12])
    )


def is_external_bot_speaker(speaker_id: str) -> bool:
    """检查发言人是否属于配置的同群外部机器人名单。"""
    sid = (speaker_id or "").strip()
    if not sid:
        return False
    from gsuid_core.ai_core.memory.config import memory_config

    return sid in set(memory_config.external_bot_ids)


def is_ai_list_allowed(speaker_id: str, group_id: Optional[str]) -> bool:
    """同步校验 ai_config 的 black_list / white_list，阻断已拉黑群与用户。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    raw_black = ai_config.get_config("black_list").data
    raw_white = ai_config.get_config("white_list").data
    black = [str(item).strip() for item in raw_black if str(item).strip()] if isinstance(raw_black, list) else []
    white = [str(item).strip() for item in raw_white if str(item).strip()] if isinstance(raw_white, list) else []
    sid = (speaker_id or "").strip()
    gid = (group_id or "").strip()
    if black and ((sid and sid in black) or (gid and gid in black)):
        return False
    if white and not ((sid and sid in white) or (gid and gid in white)):
        return False
    return True


def _gate(
    content: str,
    speaker_id: str,
    bot_self_id: str,
    observer_blacklist: list[str],
    group_id: Optional[str],
    scope_key: str,
) -> Optional[str]:
    """协议级与生产防污染网关。返回 "HIGH" 或 None。"""
    if not (speaker_id or "").strip() or _is_known_bot_speaker(speaker_id, bot_self_id):
        return None
    if is_external_bot_speaker(speaker_id):
        logger.trace(t("log.memory.observer_external_bot_filtered", p0=speaker_id))
        return None
    if group_id and group_id in observer_blacklist:
        return None
    if not is_ai_list_allowed(speaker_id, group_id):
        return None
    stripped = content.strip()
    if not stripped:
        return None
    if _is_system_or_error_echo(stripped):
        return None
    if stripped.startswith("[图片]") and "img_" not in stripped and len(stripped) < 10:
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
    reply_to_bot: bool = False,
    personal_credential: bool = False,
    is_proactive_broadcast: bool = False,
) -> None:
    """向观察队列投递一条消息记录（不 await 调用为主）。"""
    from .scope import ScopeType, make_scope_key, self_scope_key

    if is_proactive_broadcast:
        return

    if message_id:
        from gsuid_core.message_history import get_history_manager

        if get_history_manager().is_command_hit(message_id):
            return

    bsid = (bot_self_id or "").strip()
    if bsid:
        _KNOWN_BOT_SELF_IDS.add(bsid)

    is_self_speech = force_scope_key is None and speaker_id.startswith(_ASSISTANT_PREFIX)
    if is_self_speech:
        if group_id and group_id in observer_blacklist:
            return
        if not is_ai_list_allowed("", group_id):
            return
        stripped = content.strip()
        if not stripped or _is_system_or_error_echo(stripped):
            return
        # 群聊助手发言写回本群 scope，私聊助手发言写回对应用户 scope，不再污染全局 self:
        if group_id:
            scope_key = make_scope_key(ScopeType.GROUP, group_id)
        elif at_targets and at_targets[0].strip():
            scope_key = make_scope_key(ScopeType.USER_GLOBAL, at_targets[0].strip())
        elif message_type == "private_msg":
            logger.debug(t("log.memory.observer_skip_dm_assistant_no_target"))
            return
        else:
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

    if is_scope_frozen(scope_key):
        return

    if not is_self_speech and group_id:
        from gsuid_core.ai_core.mcp.pii import mask_pii

        content = mask_pii(content)

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
        reply_to_bot=reply_to_bot,
        bot_id=bot_id,
        persona_name=persona_name,
        personal_credential=personal_credential,
        is_assistant_context=is_self_speech,
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
                "speaker_ids": [str(rec.speaker_id)] if rec.speaker_id else [],
            }
        )
    return episodes
