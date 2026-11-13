"""
消息历史的 AI 格式化工具

将 gsuid_core.message_history 中的通用消息记录（MessageRecord）转换为
AI 可用的 prompt / messages / Agent 上下文格式。

本模块依赖通用消息历史模块，方向为 ai_core -> message_history，
通用消息历史模块本身不感知这些 AI 格式化逻辑。
"""

from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Sequence
from datetime import datetime

from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.message_history import MessageRecord


def history_to_prompt(
    history: List[MessageRecord],
    include_system: bool = True,
    format_template: Optional[str] = None,
) -> str:
    """
    将历史记录转换为AI可用的prompt字符串

    Args:
        history: 消息记录列表
        include_system: 是否包含system消息
        format_template: 自定义格式模板，默认使用标准格式
            模板变量: {role}, {content}, {timestamp}, {index}, {user_id}, {user_name}

    Returns:
        格式化后的prompt字符串

    Example:
        >>> history = manager.get_history(event)
        >>> prompt = history_to_prompt(history)
        >>> # 输出格式:
        >>> # [用户-123]: 你好
        >>> # [AI]: 你好！有什么可以帮助你的吗？
    """
    if not history:
        return ""

    if format_template:
        lines = []
        for i, record in enumerate(history, 1):
            if record.role == "system" and not include_system:
                continue
            is_cmd = bool(record.metadata and record.metadata.get("command_hit"))
            rec_content = "[用户调用了 bot 功能]" if is_cmd else record.content
            line = format_template.format(
                role=record.role,
                content=rec_content,
                timestamp=record.timestamp,
                index=i,
                user_id=record.user_id,
                user_name=record.user_name or "",
            )
            lines.append(line)
        return str("\n".join(lines))

    # 默认格式
    role_display = {
        "user": "[用户",
        "assistant": "[AI]",
        "system": "[系统]",
    }

    lines = []
    for record in history:
        if record.role == "system" and not include_system:
            continue

        if record.role == "user":
            user_label = record.user_name or record.user_id
            is_cmd = bool(record.metadata and record.metadata.get("command_hit"))
            c_text = "[用户调用了 bot 功能]" if is_cmd else record.content
            lines.append(f"[用户-{user_label}]: {c_text}")
        else:
            role_label = role_display.get(record.role, f"[{record.role}]")
            lines.append(f"{role_label}: {record.content}")

    return str("\n".join(lines))


def history_to_messages(
    history: List[MessageRecord],
    include_system: bool = True,
) -> List[Dict[str, str]]:
    """
    将历史记录转换为OpenAI格式的messages列表

    Args:
        history: 消息记录列表
        include_system: 是否包含system消息

    Returns:
        OpenAI格式的messages列表

    Example:
        >>> history = manager.get_history(event)
        >>> messages = history_to_messages(history)
        >>> # 输出: [{"role": "user", "content": "你好"}, ...]
    """
    messages = []

    for record in history:
        if record.role == "system" and not include_system:
            continue

        c_text = "[用户调用了 bot 功能]" if (record.metadata and record.metadata.get("command_hit")) else record.content
        messages.append(
            {
                "role": record.role,
                "content": c_text,
            }
        )

    return messages


def _format_timestamp(ts: float, ref_ts: Optional[float] = None) -> str:
    """
    将 Unix 时间戳格式化为对模型友好的时间字符串。

    策略（以 ref_ts 为"当前时间"基准，默认用 time.time()）：
        - 今天内         → "HH:MM:SS"
        - 昨天           → "昨天 HH:MM:SS"
        - 今年内（非昨天）→ "M月D日 HH:MM:SS"
        - 跨年           → "YYYY年M月D日 HH:MM:SS"
    """
    if ref_ts is None:
        ref_ts = time.time()

    msg_dt = datetime.fromtimestamp(ts)
    ref_dt = datetime.fromtimestamp(ref_ts)

    msg_date = msg_dt.date()
    ref_date = ref_dt.date()
    delta_days = (ref_date - msg_date).days

    time_str = msg_dt.strftime("%H:%M:%S")

    if delta_days == 0:
        return time_str
    elif delta_days == 1:
        return f"昨天 {time_str}"
    elif msg_dt.year == ref_dt.year:
        return f"{msg_dt.month}月{msg_dt.day}日 {time_str}"
    else:
        return f"{msg_dt.year}年{msg_dt.month}月{msg_dt.day}日 {time_str}"


# 用户连发消息合并窗口（秒）：窗口内同用户的相邻消息在历史中合并为一个发言块。
def _merge_window() -> float:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return float(ai_config.get_config("history_merge_window").data)


# 出站产物句柄。不含 to_/sa_：那些是检索落盘，不是对这个人发出去的内容。
_THREAD_HANDLE_RE = re.compile(r"\b(?:res|img|aud|vid|fil)_[0-9a-fA-F]{6,}\b|\bdlg_[0-9a-fA-F-]{8,}\b")
SPEAKER_THREAD_LIMIT = 14
_THREAD_HANDLES_KEY = "thread_handles"
_MAX_NOTED_HANDLES = 4
_RECEIPT_CHANNELS: frozenset[str] = frozenset({"command", "tool_delivery"})


def outbound_history_fields(
    present_keys: set[str],
    *,
    target_type: str,
    sender_id: str,
) -> dict[str, str]:
    """群出站补收件人。调用方已经写过的键不覆盖。"""
    extra: dict[str, str] = {}
    if target_type != "direct" and sender_id and "reply_to_user_id" not in present_keys:
        extra["reply_to_user_id"] = sender_id
    if "speech_channel" not in present_keys:
        extra["speech_channel"] = "command"
    return extra


def _speech_channel(record: MessageRecord) -> str:
    meta = record.metadata or {}
    raw = meta["speech_channel"] if "speech_channel" in meta else ""
    if isinstance(raw, str) and raw:
        return raw
    return "persona"


def is_persona_speech(record: MessageRecord) -> bool:
    """人格自己说的话；指令回执与工具交付不算。"""
    return record.role == "assistant" and _speech_channel(record) not in _RECEIPT_CHANNELS


def extract_thread_handles(text: str) -> List[str]:
    """正文里的出站句柄，去重保序。"""
    found: List[str] = []
    for hid in _THREAD_HANDLE_RE.findall(text or ""):
        if hid not in found:
            found.append(hid)
    return found


def _handles_from_metadata(record: MessageRecord) -> List[str]:
    meta = record.metadata
    if "outbound_handles" not in meta:
        return []
    raw = meta["outbound_handles"]
    if not isinstance(raw, list):
        return []
    found: List[str] = []
    for item in raw:
        if isinstance(item, str) and item and item not in found:
            found.append(item)
    return found


def format_private_recent_history(records: Sequence[MessageRecord]) -> str:
    """私聊冷启动时格式化最近历史记录（线程视图窗口）。"""
    if not records:
        return ""
    from gsuid_core.ai_core.thread_view import render_window

    return render_window(records)


def _read_handle_list(ctx: ToolContext) -> List[str]:
    extra = ctx.extra
    if _THREAD_HANDLES_KEY not in extra:
        return []
    raw = extra[_THREAD_HANDLES_KEY]
    if not isinstance(raw, list):
        return []
    found: List[str] = []
    for item in raw:
        if isinstance(item, str) and item not in found:
            found.append(item)
    return found


def note_thread_handle(ctx: ToolContext, handle: str) -> None:
    """本轮出站句柄记在 ToolContext 上，静默收尾时写入群历史。"""
    hid = handle.strip().strip("`")
    if _THREAD_HANDLE_RE.fullmatch(hid) is None:
        return
    current = _read_handle_list(ctx)
    if hid in current or len(current) >= _MAX_NOTED_HANDLES:
        return
    current.append(hid)
    ctx.extra[_THREAD_HANDLES_KEY] = current


def note_thread_handles_in_text(ctx: ToolContext, text: str) -> None:
    for hid in extract_thread_handles(text):
        note_thread_handle(ctx, hid)


def noted_thread_handles(ctx: Optional[ToolContext]) -> List[str]:
    if ctx is None:
        return []
    return _read_handle_list(ctx)


def remember_silence_handles(
    event: Event,
    handles: Sequence[str],
    *,
    manager: object = None,
) -> None:
    """静默轮把句柄写入 A 轨。已出现在近期出站里的不重复记。"""
    if not event.group_id:
        return
    fresh: List[str] = []
    for hid in handles:
        if hid and hid not in fresh and _THREAD_HANDLE_RE.fullmatch(hid) is not None:
            fresh.append(hid)
        if len(fresh) >= _MAX_NOTED_HANDLES:
            break
    if not fresh:
        return
    from gsuid_core.message_history.manager import HistoryManager

    mgr: HistoryManager
    if isinstance(manager, HistoryManager):
        mgr = manager
    else:
        from gsuid_core.message_history import get_history_manager

        mgr = get_history_manager()
    recorded: List[str] = []
    for record in mgr.get_history(event):
        if record.role != "assistant":
            continue
        recorded.extend(extract_thread_handles(record.content))
        recorded.extend(_handles_from_metadata(record))
    kept = [hid for hid in fresh if hid not in recorded]
    if not kept:
        return
    from gsuid_core.ai_core.agent_run.support import turn_reply_metadata

    meta: dict[str, object] = {"outbound_handles": kept}
    meta.update(turn_reply_metadata(event))
    mgr.add_message(
        event=event,
        role="assistant",
        content=" ".join(kept),
        user_name="AI",
        metadata=meta,
    )
