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
            line = format_template.format(
                role=record.role,
                content=record.content,
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
            lines.append(f"[用户-{user_label}]: {record.content}")
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

        messages.append(
            {
                "role": record.role,
                "content": record.content,
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


# 同一用户连发多段消息的合并窗口（秒）：窗口内的相邻同人消息在历史里合并为
# 一个发言块，让"@某人"+"醒了吗"这类拆条连发对模型呈现为一句完整的话。
# 窗口值唯一来源是 ai_config `history_merge_window`（可在线调）。
def _merge_window() -> float:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return float(ai_config.get_config("history_merge_window").data)


# 出站产物句柄。不含 to_/sa_：那些是检索落盘，不是对这个人发出去的内容。
_THREAD_HANDLE_RE = re.compile(r"\b(?:res|img|aud|vid)_[0-9a-fA-F]{6,}\b|\bdlg_[0-9a-fA-F-]{8,}\b")
SPEAKER_THREAD_LIMIT = 14
SPEAKER_THREAD_HEADER = "[与你的对话] 旧→新"
_OTHERS_WINDOW = 20
_OTHERS_LIMIT = 6
_THREAD_HANDLES_KEY = "thread_handles"
_MAX_NOTED_HANDLES = 4
_HANDLE_LINE_SUFFIX = "（read_handle；句柄勿念出）"


def format_history_for_agent(
    history: List[MessageRecord],
    current_user_id: Optional[str] = None,
    current_user_name: Optional[str] = None,
    *,
    include_current_turn: bool = False,
    block_header: str = "[历史对话] 旧→新",
    content_limit: int = 280,
) -> str:
    """
    将历史记录格式化为 Agent 可用的紧凑上下文。

    默认只输出 ``[历史对话]`` 块（紧凑单行），**不含**当前触发消息——当前消息已由
    ``prepare_content_payload`` 放在 ``[用户发言]`` 里，再抽一遍会双重占用 token。

    输出示例（时间在前、说话人与本轮发言同形 ``名(用户ID:id)``）::

        [历史对话] 旧→新
        [14:32:05] 小明(用户ID:456): 今天天气怎么样？
        [14:32:18] AI: 嗯，晴天
        [昨天 22:10:03] 用户ID:789: 大家好

    特殊处理：
        - 时间戳含秒：今天 ``HH:MM:SS``，跨日/跨年逐级补全；时间置前便于扫时间线
        - 说话人标签与 ``prepare_content_payload`` 统一为 ``名(用户ID:id)``
        - 同人合并窗口内连发合并为一个发言块，正文用「 / 」拼接
        - at/图/音/文件压成行尾 ``| …``，不另起 ``---`` 大块
        - ``include_current_turn=True`` 时才把 current_user 最后一条标 ``[当前]`` 前置（heartbeat 等）
    """
    if not history:
        return ""

    ref_ts = time.time()

    # include_current_turn 时：把 current_user 最后一条 user 记前置（不进历史块）
    current_record_index: Optional[int] = None
    if include_current_turn and current_user_id is not None:
        for i in range(len(history) - 1, -1, -1):
            r = history[i]
            if r.role == "user" and r.user_id == current_user_id:
                current_record_index = i
                break

    def _user_label(user_id: str, user_name: Optional[str]) -> str:
        # 与本轮 [用户发言] 头一致：名(用户ID:id)，避免历史 id(名) / 当前 名(用户ID:id) 双轨
        if user_name and str(user_name).strip() and str(user_name).strip() != str(user_id):
            return f"{str(user_name).strip()}(用户ID:{user_id})"
        return f"用户ID:{user_id}"

    name_map: Dict[str, str] = {}
    for r in history:
        if r.role == "user" and r.user_name:
            name_map[str(r.user_id)] = r.user_name

    def _meta_suffix(record: MessageRecord) -> str:
        """图片/@/文件等元数据压缩成行尾，避免 --- 分隔块打断时间线阅读。"""
        bits: List[str] = []
        metadata = record.metadata or {}
        from gsuid_core.ai_core.interaction_scaffold import AT_OTHER_MARKER

        at_ids = metadata["at_list"] if "at_list" in metadata else []
        from gsuid_core.ai_core.configs.ai_config import ai_config

        _at_max = int(ai_config.get_config("group_at_list_max").data)
        if isinstance(at_ids, list) and _at_max > 0 and len(at_ids) > _at_max:
            bits.append(f"@×{len(at_ids)}")
        elif isinstance(at_ids, list):
            for at_id in at_ids:
                at_key = str(at_id)
                at_label = _user_label(at_key, name_map[at_key] if at_key in name_map else None)
                bits.append(f"@{at_label}{AT_OTHER_MARKER}")
        image_id = metadata.get("image_id")
        if image_id:
            bits.append(f"图:{image_id}")
        for img_id in metadata.get("image_id_list", []):
            bits.append(f"图:{img_id}")
        audio_id = metadata.get("audio_id")
        if audio_id:
            bits.append(f"音频:{audio_id}")
        file_id = metadata.get("file_id")
        if file_id:
            bits.append(f"文件:{file_id}")
        return (" | " + " ".join(bits)) if bits else ""

    def _content_one_line(record: MessageRecord) -> str:
        content = record.content.strip().replace("\n", " / ")
        # 句柄行不能按普通正文裁，否则 read_handle 拿到半个 id。
        if _THREAD_HANDLE_RE.search(content):
            if len(content) > 400:
                return content[:397] + "…"
            return content
        if len(content) > content_limit:
            return content[: content_limit - 1] + "…"
        return content

    def _render_group(records: List[MessageRecord], speaker: str) -> str:
        """同人连发合并：``[时间] 说话人: 正文 | 元数据``。"""
        ts = _format_timestamp(records[0].timestamp, ref_ts)
        bodies = [_content_one_line(r) for r in records if r.content.strip()]
        # 元数据按条收集后去重拼接，避免连发多图重复成串
        meta_bits: List[str] = []
        for r in records:
            m = _meta_suffix(r)
            if m and m not in meta_bits:
                meta_bits.append(m)
        metas = "".join(meta_bits)
        body = " / ".join(bodies) if bodies else ""
        return f"[{ts}] {speaker}: {body}{metas}".rstrip()

    def _make_speaker(record: MessageRecord) -> str:
        if record.role == "assistant":
            reply_to = None
            reply_name = None
            if record.metadata:
                reply_to = record.metadata.get("reply_to_user_id")
                reply_name = record.metadata.get("reply_to_user_name")
            if reply_to:
                return f"AI→{_user_label(str(reply_to), reply_name)}"
            return "AI"
        return _user_label(record.user_id, record.user_name)

    output: List[str] = []

    if current_record_index is not None and current_user_id:
        current_record = history[current_record_index]
        name = current_user_name or current_record.user_name
        base_label = _user_label(current_user_id, name)
        output.append(_render_group([current_record], f"当前·{base_label}"))

    history_lines: List[str] = []
    pending_group: List[MessageRecord] = []
    merge_window = _merge_window()

    def _flush_group() -> None:
        if not pending_group:
            return
        history_lines.append(_render_group(pending_group, _make_speaker(pending_group[0])))
        pending_group.clear()

    from gsuid_core.ai_core.utils import is_silence_marker

    for i, record in enumerate(history):
        if record.role == "system":
            continue
        if i == current_record_index:
            continue
        if record.role == "assistant" and is_silence_marker(record.content):
            continue

        if (
            pending_group
            and record.role == "user"
            and pending_group[-1].role == "user"
            and str(record.user_id) == str(pending_group[-1].user_id)
            and 0 <= record.timestamp - pending_group[0].timestamp <= merge_window
        ):
            pending_group.append(record)
            continue

        _flush_group()
        pending_group.append(record)

    _flush_group()

    if history_lines:
        output.append(block_header)
        output.extend(history_lines)

    return "\n".join(output)


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


def _handle_line(handles: Sequence[str]) -> str:
    shown = [h for h in handles if h][:_MAX_NOTED_HANDLES]
    return f"{' '.join(shown)}{_HANDLE_LINE_SUFFIX}"


def _speech_residue(content: str) -> str:
    text = _THREAD_HANDLE_RE.sub("", content)
    text = re.sub(r"\[图片[^\]]*\]", "", text)
    text = text.replace(_HANDLE_LINE_SUFFIX, "")
    return re.sub(r"[\s|（）()：:，,。…·]+", "", text)


def assistant_visible_body(record: MessageRecord) -> Optional[str]:
    """出站在同人线程里的正文。纯静默且无句柄返回 None，不占 14 条名额。"""
    content = record.content.strip()
    handles = _handles_from_metadata(record)
    for hid in extract_thread_handles(content):
        if hid not in handles:
            handles.append(hid)
    if "read_handle" in content and handles:
        return content
    from gsuid_core.ai_core.utils import is_silence_marker

    if is_silence_marker(content):
        if not handles:
            return None
        return _handle_line(handles)
    if not content and not handles:
        return None
    if handles and len(_speech_residue(content)) < 4:
        return _handle_line(handles)
    if handles:
        speech = _THREAD_HANDLE_RE.sub("", content)
        speech = re.sub(r"\[图片[^\]]*\]", "", speech)
        speech = re.sub(r"\s+", " ", speech).strip(" |")
        if speech:
            return f"{speech} | {_handle_line(handles)}"
        return _handle_line(handles)
    return content


def select_speaker_thread(
    records: Sequence[MessageRecord],
    current_user_id: str,
    *,
    limit: int = SPEAKER_THREAD_LIMIT,
) -> List[MessageRecord]:
    """从新到旧凑满 limit：当前说话人的 user 句（含未点名）与出站。不成对。"""
    uid = str(current_user_id)
    picked: List[MessageRecord] = []
    for record in reversed(list(records)):
        if record.role == "user" and str(record.user_id) == uid:
            picked.append(record)
        elif record.role == "assistant" and assistant_visible_body(record) is not None:
            picked.append(record)
        if len(picked) >= limit:
            break
    picked.reverse()
    return picked


def materialize_speaker_thread(records: Sequence[MessageRecord]) -> List[MessageRecord]:
    """静默格换成句柄行；没有句柄的静默丢掉。"""
    out: List[MessageRecord] = []
    for record in records:
        if record.role != "assistant":
            out.append(record)
            continue
        body = assistant_visible_body(record)
        if body is None:
            continue
        if body == record.content:
            out.append(record)
            continue
        out.append(
            MessageRecord(
                role=record.role,
                content=body,
                user_id=record.user_id,
                user_name=record.user_name,
                user_avatar=record.user_avatar,
                timestamp=record.timestamp,
                metadata=record.metadata,
            )
        )
    return out


def compose_group_history(
    records: Sequence[MessageRecord],
    *,
    current_user_id: str,
    current_user_name: Optional[str] = None,
) -> str:
    """同人线程在前（裁预算时先保住句柄），旁人块仍是最近窗里的他人 user 句。"""
    if not records:
        return ""
    uid = str(current_user_id)
    recent = list(records)[-_OTHERS_WINDOW:]
    others = [r for r in recent if r.role == "user" and str(r.user_id) != uid][-_OTHERS_LIMIT:]
    thread = materialize_speaker_thread(select_speaker_thread(records, uid))
    parts: List[str] = []
    if thread:
        parts.append(
            format_history_for_agent(
                thread,
                current_user_id=uid,
                current_user_name=current_user_name,
                block_header=SPEAKER_THREAD_HEADER,
                content_limit=160,
            )
        )
    if others:
        parts.append(
            format_history_for_agent(
                others,
                current_user_id=uid,
                current_user_name=current_user_name,
            )
        )
    return "\n\n".join(parts)


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
    mgr.add_message(
        event=event,
        role="assistant",
        content=" ".join(kept),
        user_name="AI",
        metadata={"outbound_handles": kept},
    )
