"""按词或时间点读取群 / 私聊发言。库存是最近 12 小时，不一次倒出。"""

from __future__ import annotations

import time
from datetime import datetime
from dataclasses import replace

from pydantic_ai import RunContext

from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.message_history import MessageRecord
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.history_format import format_history_for_agent
from gsuid_core.message_history.manager import HistoryManager

_RESULT_CAP = 40
_RADIUS_MAX = 120
_HIT_CONTENT_LIMIT = 4000
_AT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M")


def _bot_prefix(ev: Event) -> str:
    parts = ev.session_id.split(":")
    if len(parts) < 3:
        return ""
    return ":".join(parts[:3])


def _same_bot(current: Event, other: Event) -> bool:
    prefix = _bot_prefix(current)
    return bool(prefix) and prefix == _bot_prefix(other)


def _wanted_session_id(current: Event, ref: str) -> str:
    raw = ref.strip()
    if not raw:
        return current.session_id
    if raw.startswith("group:") or raw.startswith("private:"):
        prefix = _bot_prefix(current)
        if not prefix:
            return raw
        return f"{prefix}:{raw}"
    return raw


def _is_private(ev: Event) -> bool:
    return ev.user_type == "direct"


def _may_read(current: Event, target: Event, *, is_master: bool) -> bool:
    if not _same_bot(current, target):
        return False
    if not _is_private(target):
        return True
    if is_master:
        return True
    return str(target.user_id) == str(current.user_id)


def _find_session(manager: HistoryManager, current: Event, ref: str) -> Event | None:
    want = _wanted_session_id(current, ref)
    for ev in manager.list_sessions():
        if ev.session_id == want and _same_bot(current, ev):
            return ev
    return None


def _session_label(ev: Event) -> str:
    if _is_private(ev):
        return f"private:{ev.user_id}"
    return f"group:{ev.group_id or ''}"


def _list_sessions(manager: HistoryManager, current: Event, *, is_master: bool) -> str:
    lines: list[str] = []
    for ev in manager.list_sessions():
        if not _may_read(current, ev, is_master=is_master):
            continue
        records = manager.get_history(ev)
        if not records:
            continue
        lines.append(f"{_session_label(ev)} · {len(records)}条")
    if not lines:
        return "当前机器人没有留下可看的会话。"
    return "【可看的会话】\n" + "\n".join(lines)


def _parse_at(raw: str, now: float) -> float | None:
    """解析 HH:MM 或带日期的时间。只有钟点且落在未来时，算到前一天。"""
    text = raw.strip()
    if not text:
        return None
    for fmt in _AT_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:
            lt = time.localtime(now)
            parsed = parsed.replace(year=lt.tm_year, month=lt.tm_mon, day=lt.tm_mday)
            stamp = time.mktime(parsed.timetuple())
            if stamp > now + 60:
                stamp -= 86400
            return stamp
        return time.mktime(parsed.timetuple())
    return None


def _result_cap(limit: int) -> int:
    if limit < 1:
        return _RESULT_CAP
    if limit > 80:
        return 80
    return limit


def _radius_seconds(radius_minutes: int) -> int:
    span = radius_minutes
    if span < 1:
        span = 1
    elif span > _RADIUS_MAX:
        span = _RADIUS_MAX
    return span * 60


def _query_hit(rec: MessageRecord, needle: str) -> bool:
    if not needle:
        return True
    hay = f"{rec.user_name or ''} {rec.content}".casefold()
    folded = needle.casefold()
    if folded in hay:
        return True
    parts = [part for part in folded.split() if part]
    return len(parts) > 1 and all(part in hay for part in parts)


def _first_match_span(hay: str, needle: str) -> tuple[int, int]:
    folded = needle.casefold().strip()
    if not folded:
        return -1, 0
    idx = hay.find(folded)
    if idx >= 0:
        return idx, len(folded)
    best = -1
    match_len = 0
    for part in folded.split():
        if not part:
            continue
        at = hay.find(part)
        if at >= 0 and (best < 0 or at < best):
            best = at
            match_len = len(part)
    return best, match_len


def _window_content(content: str, needle: str, limit: int) -> str:
    """长文只留命中附近。条数上限控体积，这里保证 query 命中不被头裁掉。"""
    if limit < 2 or len(content) <= limit:
        return content
    idx, match_len = _first_match_span(content.casefold(), needle)
    if idx < 0:
        return content[: limit - 1] + "…"
    inner = limit - 2
    if inner < 1:
        inner = 1
    if match_len >= inner:
        chunk = content[idx : idx + inner]
        prefix = "…" if idx > 0 else ""
        suffix = "…" if idx + inner < len(content) else ""
        return prefix + chunk + suffix
    left = (inner - match_len) // 2
    start = idx - left
    if start < 0:
        start = 0
    end = start + inner
    if end > len(content):
        end = len(content)
        start = end - inner if end > inner else 0
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(content) else ""
    return prefix + content[start:end] + suffix


def _clip_hit_record(rec: MessageRecord, needle: str) -> MessageRecord:
    clipped = _window_content(rec.content, needle, _HIT_CONTENT_LIMIT)
    if clipped == rec.content:
        return rec
    return replace(rec, content=clipped)


def _select_hits(
    records: list[MessageRecord],
    *,
    query: str,
    center: float | None,
    radius_s: int,
    cap: int,
) -> tuple[list[MessageRecord], int]:
    matched = records
    if center is not None:
        lo = center - radius_s
        hi = center + radius_s
        matched = [rec for rec in matched if lo <= rec.timestamp <= hi]
    needle = query.strip()
    if needle:
        matched = [rec for rec in matched if _query_hit(rec, needle)]
    total = len(matched)
    if center is not None:
        nearest = sorted(matched, key=lambda rec: (abs(rec.timestamp - center), rec.timestamp))
        picked = nearest[:cap]
    else:
        picked = matched[-cap:]
    picked.sort(key=lambda rec: rec.timestamp)
    return picked, total


def read_chat_history_text(
    manager: HistoryManager,
    current: Event,
    session_id: str = "",
    *,
    query: str = "",
    at: str = "",
    radius_minutes: int = 15,
    limit: int = _RESULT_CAP,
    is_master: bool = False,
    now: float | None = None,
) -> str:
    """列会话，或在 12 小时库存里按词 / 时间点取发言。不含图片像素。"""
    ref = (session_id or "").strip()
    if ref == "list":
        return _list_sessions(manager, current, is_master=is_master)
    target = _find_session(manager, current, ref)
    if target is None:
        return "没有这个会话的发言记录。session_id 用 list、group:群号 或 private:用户ID。"
    if not _may_read(current, target, is_master=is_master):
        return "别人的私聊记录不能读。"
    records = manager.get_history(target)
    label = _session_label(target)
    if not records:
        return f"{label} 最近12小时没有留下发言。"
    needle = query.strip()
    clock = at.strip()
    if not needle and not clock:
        return f"{label} · 最近12小时 {len(records)} 条。用 query 搜原文，或 at=HH:MM 看前后一段时间。不会一次倒出。"
    moment = time.time() if now is None else now
    center: float | None = None
    if clock:
        parsed = _parse_at(clock, moment)
        if parsed is None:
            return "时间写不清。用 HH:MM，或 YYYY-MM-DD HH:MM。"
        center = parsed
    picked, total = _select_hits(
        records,
        query=needle,
        center=center,
        radius_s=_radius_seconds(radius_minutes),
        cap=_result_cap(limit),
    )
    if total == 0:
        if center is not None and not needle:
            return f"{label} 这个时间附近没有发言。"
        return f"{label} 没有命中。换个词，或用 at=HH:MM 指定时间。"
    shown = [_clip_hit_record(rec, needle) for rec in picked]
    body = format_history_for_agent(
        shown,
        block_header="[会话记录] 旧→新",
        content_limit=_HIT_CONTENT_LIMIT * 2,
    )
    head = f"{label} · 命中 {total} 条"
    if total > len(picked):
        head += f"，这里是其中 {len(picked)} 条"
    return f"{head}（发言文本，不含图片内容）\n{body}"


@ai_tools(category="buildin")
async def read_chat_history(
    ctx: RunContext[ToolContext],
    session_id: str = "",
    query: str = "",
    at: str = "",
    radius_minutes: int = 15,
    limit: int = 40,
) -> str:
    """在最近 12 小时的群或私聊记录里搜索，不一次读出全部。

    session_id 留空是当前会话。``list`` 只列会话和条数。
    ``group:群号`` / ``private:用户ID`` 指定会话。别人的私聊只有主人能读。
    query 按原文搜；at 用 HH:MM 或 YYYY-MM-DD HH:MM，看该时刻前后 radius_minutes 分钟。
    两者都空时只告诉条数。返回发言文本，不含图片内容。

    Args:
        ctx: 工具执行上下文
        session_id: 空=当前；list=列会话；group:/private:/完整 session id
        query: 要搜的词，可多个（都要出现）
        at: 时间点。HH:MM，或 YYYY-MM-DD HH:MM
        radius_minutes: at 前后各多少分钟，默认 15，最大 120
        limit: 最多返回条数，默认 40，最大 80
    """
    ev = ctx.deps.ev
    if ev is None:
        return "没有当前会话，无法读取。"
    from gsuid_core.ai_core.utils import _is_master_user
    from gsuid_core.message_history import get_history_manager

    return read_chat_history_text(
        get_history_manager(),
        ev,
        session_id,
        query=query,
        at=at,
        radius_minutes=radius_minutes,
        limit=limit,
        is_master=_is_master_user(str(ev.user_id)),
    )
