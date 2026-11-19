"""线程视图渲染器：群聊/私聊时间线 → 单一块文本。

主 Agent、TurnRouter、heartbeat、WebConsole 诊断共用本模块，禁止各自拼历史文本。
纯同步函数，输入 Sequence[MessageRecord]，无 LLM。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence
from datetime import datetime
from dataclasses import dataclass

from gsuid_core.ai_core.utils import is_silence_marker, _strip_resource_handles
from gsuid_core.message_history import MessageRecord
from gsuid_core.ai_core.control.lines import (
    THREAD_AT_MARK,
    ROUTER_PRINCIPLE,
    THREAD_SENT_MARK,
    THREAD_REPLY_MARK,
    THREAD_RECEIPT_TAG,
    ROUTER_PENDING_HEAD,
    ROUTER_TIMELINE_HEAD,
    thread_view_header,
    omitted_lines_marker,
)
from gsuid_core.ai_core.history_format import (
    _RECEIPT_CHANNELS,
    _speech_channel,
    _format_timestamp,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.inbox.turn import InboxTurn
    from gsuid_core.ai_core.inbox.state import PendingEntry

_PERSONA_SPEECH_LABEL = "闲聊回复"
_MAX_ANCHOR_CHARS = 60
_MAX_RECEIPT_CHARS = 280
_BACKFILL_LINES = 16
_QUOTE_HOPS = 3
_SLICE_RECENT_WINDOW_S = 180.0
_SLICE_MAX_AGE_S = 1800.0
_SLICE_SELF_PRE_LIMIT = 3
_SLICE_GROUP_RECENT_LIMIT = 6
_SPAM_REPEAT_MIN = 3
_ROUTER_RECENT_LIMIT = 4


@dataclass(frozen=True, slots=True)
class _Line:
    """一行渲染结果；seqs 为该行覆盖的源记录 seq。"""

    first: int
    last: int
    seqs: tuple[int, ...]
    text: str


def _plain_name(record: MessageRecord) -> str:
    name = record.user_name.strip() if record.user_name else ""
    if name and name != str(record.user_id):
        return name
    return f"用户ID:{record.user_id}"


def _speaker_label(record: MessageRecord) -> str:
    from gsuid_core.ai_core.utils import _build_relationship_description

    return _build_relationship_description(record.user_name, str(record.user_id))


def _compact_speaker_label(record: MessageRecord) -> str:
    """TurnRouter 专用紧凑称呼：省掉 (用户ID:xxxx) 降 token，保留主人标识。"""
    from gsuid_core.ai_core.utils import _is_master_user

    name = _plain_name(record)
    if _is_master_user(str(record.user_id)):
        return f"[⚡主人] {name}"
    return name


def _ctx_speaker(record: MessageRecord, ctx: "_RenderCtx") -> str:
    return _compact_speaker_label(record) if ctx.compact else _speaker_label(record)


def _one_line(text: str, limit: int) -> str:
    body = " / ".join(part for part in text.splitlines() if part.strip())
    if len(body) > limit:
        return body[:limit] + "…"
    return body


def _guard_body(text: str) -> str:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("content_guard_enable").data:
        return text
    from gsuid_core.ai_core.content_guard import annotate_untrusted_message_ex

    body, _flags = annotate_untrusted_message_ex(text)
    return body


def _ago(delta_s: float) -> str:
    seconds = int(max(0.0, delta_s))
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // 3600} 小时前"


def _is_own_speech(record: MessageRecord, persona_name: str | None) -> bool:
    """记录是否算「你」的发言；persona 缺省或吻合都算。"""
    raw = record.metadata["persona_name"] if "persona_name" in record.metadata else None
    if persona_name is None or raw is None:
        return True
    return str(raw) == persona_name


def _you(record: MessageRecord, persona_name: str | None) -> str:
    return "你" if _is_own_speech(record, persona_name) else _plain_name(record)


def _name_of(records: dict[int, MessageRecord], seq: int | None) -> str:
    if seq is None:
        return ""
    if seq not in records:
        return ""
    return _plain_name(records[seq])


def _quote_marker(
    record: MessageRecord,
    seq_index: dict[int, MessageRecord],
    persona_name: str | None,
) -> str:
    link = record.link
    if link is None or link.reply_to_seq is None:
        meta = record.metadata
        if "reply_preview" in meta:
            return f" {THREAD_REPLY_MARK}(引用: {_one_line(str(meta['reply_preview']), 40)})"
        return ""
    target = seq_index[link.reply_to_seq] if link.reply_to_seq in seq_index else None
    if target is None:
        meta = record.metadata
        if "reply_preview" in meta:
            return f" {THREAD_REPLY_MARK}(引用: {_one_line(str(meta['reply_preview']), 40)})"
        return f" {THREAD_REPLY_MARK}(引用: #{link.reply_to_seq})"
    if target.role == "assistant":
        ago = _ago(record.timestamp - target.timestamp)
        if _speech_channel(target) in _RECEIPT_CHANNELS:
            who = _reply_target_name(target, seq_index)
            return f" {THREAD_REPLY_MARK}#{link.reply_to_seq}({THREAD_RECEIPT_TAG} · 由{who}触发 · {ago})"
        who = _reply_target_name(target, seq_index)
        when = datetime.fromtimestamp(target.timestamp).strftime("%H:%M")
        return (
            f" {THREAD_REPLY_MARK}#{link.reply_to_seq}"
            f"({_you(target, persona_name)}→{who} · {when} · {ago} · {_PERSONA_SPEECH_LABEL})"
        )
    return f" {THREAD_REPLY_MARK}#{link.reply_to_seq}({_plain_name(target)})"


def _name_by_uid(seq_index: dict[int, MessageRecord], uid: str) -> str:
    for rec in seq_index.values():
        if str(rec.user_id) == uid and rec.role == "user":
            return _plain_name(rec)
    return f"用户ID:{uid}"


def _reply_target_name(record: MessageRecord, seq_index: dict[int, MessageRecord]) -> str:
    link = record.link
    if link is not None and link.in_reply_to_seqs:
        name = _name_of(seq_index, link.in_reply_to_seqs[0])
        if name:
            return name
    meta = record.metadata
    if "reply_to_user_name" in meta and str(meta["reply_to_user_name"]):
        return str(meta["reply_to_user_name"])
    if "reply_to_user_id" in meta and str(meta["reply_to_user_id"]):
        return _name_by_uid(seq_index, str(meta["reply_to_user_id"]))
    return ""


def _has_structure(record: MessageRecord) -> bool:
    link = record.link
    if link is None:
        return True
    return bool(link.reply_to_seq or link.at_self or link.at_targets)


def _at_markers(record: MessageRecord, seq_index: dict[int, MessageRecord]) -> str:
    link = record.link
    if link is None:
        return ""
    parts: list[str] = []
    if link.at_self:
        parts.append(f"{THREAD_AT_MARK}你")
    for uid in link.at_targets:
        parts.append(f"{THREAD_AT_MARK}{_name_by_uid(seq_index, uid)}")
    return (" " + " ".join(parts)) if parts else ""


def _user_body(record: MessageRecord) -> str:
    """命令触发的用户消息对模型遮蔽原话（旧 format_history_for_agent 同款）。"""
    if record.metadata and "command_hit" in record.metadata and record.metadata["command_hit"]:
        return "[用户调用了 bot 功能]"
    return _one_line(_guard_body(record.content), _MAX_RECEIPT_CHARS)


def _user_line(record: MessageRecord, ctx: "_RenderCtx") -> str:
    link = record.link
    seq = link.seq if link is not None else 0
    markers = _at_markers(record, ctx.seq_index) + _quote_marker(record, ctx.seq_index, ctx.persona_name)
    return f"#{seq} [{_format_timestamp(record.timestamp)}] {_ctx_speaker(record, ctx)}{markers}: {_user_body(record)}"


def _assistant_line(record: MessageRecord, ctx: "_RenderCtx") -> str | None:
    link = record.link
    seq = link.seq if link is not None else 0
    if is_silence_marker(record.content.strip()):
        return None
    if _speech_channel(record) == "tool_delivery" and not _strip_resource_handles(record.content).strip():
        return None
    you = _you(record, ctx.persona_name)
    ts = _format_timestamp(record.timestamp)
    if _speech_channel(record) in _RECEIPT_CHANNELS:
        name = _reply_target_name(record, ctx.seq_index)
        arrow = f"→{name}" if name else ""
        return f"#{seq} [{ts}] {you}[回执]{arrow}: {_one_line(record.content, _MAX_RECEIPT_CHARS)}"
    suffix = _evidence_suffix(record, ctx)
    if link is not None and link.in_reply_to_seqs:
        n = link.in_reply_to_seqs[0]
        name = _reply_target_name(record, ctx.seq_index)
        head = f"{you}→#{n}({name})"
    else:
        head = f"{you}:"
        return f"#{seq} [{ts}] {head} {_one_line(record.content, _MAX_ANCHOR_CHARS)}{THREAD_SENT_MARK}{suffix}"
    return f"#{seq} [{ts}] {head}: {_one_line(record.content, _MAX_ANCHOR_CHARS)}{THREAD_SENT_MARK}{suffix}"


@dataclass(slots=True)
class _RenderCtx:
    seq_index: dict[int, MessageRecord]
    persona_name: str | None
    session_id: str = ""
    bot_id: str = ""
    compact: bool = False


def _evidence_suffix(record: MessageRecord, ctx: "_RenderCtx") -> str:
    """出站 persona 行的〔依据:…〕静态标签（≤2 条，绝对时间）。"""
    link = record.link
    if link is None or not link.evidence or not ctx.session_id:
        return ""
    from gsuid_core.ai_core.evidence.render import evidence_tags_for_ids

    tags = evidence_tags_for_ids(
        link.evidence,
        ctx.session_id,
        ctx.bot_id,
        lambda uid: _name_by_uid(ctx.seq_index, uid),
    )
    return "".join(tags)


def _record_line(record: MessageRecord, ctx: _RenderCtx) -> str | None:
    if record.link is None or record.role == "system":
        return None
    if record.role == "assistant":
        return _assistant_line(record, ctx)
    if record.role == "user":
        return _user_line(record, ctx)
    return None


def _merge_window_s() -> float:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    return float(ai_config.get_config("history_merge_window").data)


def _render_lines(records: Sequence[MessageRecord], ctx: _RenderCtx) -> list[_Line]:
    """按 seq 升序渲染；同人连发（无引用/@ 且间隔 ≤ merge_window）合并为一行。"""
    window = _merge_window_s()
    out: list[_Line] = []
    group: list[MessageRecord] = []

    def flush() -> None:
        if not group:
            return
        first, last = group[0], group[-1]
        first_seq = first.link.seq if first.link is not None else 0
        last_seq = last.link.seq if last.link is not None else 0
        raw_bodies = [_user_body(r) for r in group]
        parts: list[str] = []
        idx = 0
        while idx < len(raw_bodies):
            end = idx + 1
            while end < len(raw_bodies) and raw_bodies[end] == raw_bodies[idx]:
                end += 1
            if end - idx >= _SPAM_REPEAT_MIN:
                parts.append(f"{raw_bodies[idx]} (×{end - idx})")
            else:
                parts.extend(raw_bodies[idx:end])
            idx = end
        bodies = " / ".join(parts)
        seqs = tuple(r.link.seq for r in group if r.link is not None)
        if len(group) == 1:
            text = _user_line(first, ctx)
        else:
            ts = _format_timestamp(first.timestamp)
            text = f"#{first_seq}–#{last_seq} [{ts}] {_ctx_speaker(first, ctx)}: {bodies}"
        out.append(_Line(first=first_seq, last=last_seq, seqs=seqs, text=text))
        group.clear()

    for record in sorted(records, key=lambda r: r.link.seq if r.link is not None else 0):
        if record.link is None or record.role == "system":
            continue
        mergeable = (
            record.role == "user"
            and not _has_structure(record)
            and group
            and str(group[-1].user_id) == str(record.user_id)
            and 0 <= record.timestamp - group[0].timestamp <= window
        )
        if mergeable:
            group.append(record)
            continue
        flush()
        if record.role == "user" and not _has_structure(record):
            group.append(record)
        else:
            text = _record_line(record, ctx)
            if text is not None:
                out.append(_Line(first=record.link.seq, last=record.link.seq, seqs=(record.link.seq,), text=text))
    flush()
    return [line for line in out if line.text]


def _first_meta_str(records: Sequence[MessageRecord], key: str) -> str:
    for rec in records:
        if key in rec.metadata and isinstance(rec.metadata[key], str) and rec.metadata[key]:
            return str(rec.metadata[key])
    return ""


def _is_unrelated_command_noise(record: MessageRecord, target_uids: frozenset[str]) -> bool:
    if record.role == "user":
        return bool("command_hit" in record.metadata and record.metadata["command_hit"]) and (
            str(record.user_id or "") not in target_uids
        )
    if record.role == "assistant" and _speech_channel(record) in _RECEIPT_CHANNELS:
        meta = record.metadata
        uid = str(meta["reply_to_user_id"]) if "reply_to_user_id" in meta and meta["reply_to_user_id"] else ""
        return not uid or uid not in target_uids
    return False


def _select_group_slice(
    delta: list[MessageRecord],
    *,
    pending_seqs: frozenset[int],
    seq_index: dict[int, MessageRecord],
    group_recent_limit: int = _SLICE_GROUP_RECENT_LIMIT,
) -> tuple[list[MessageRecord], int]:
    """统一群聊切片：待回应 ∪ 引用链(≤3跳) ∪ 本人3分钟内前置(≤3) ∪ 群近3分钟(≤N)。"""
    if not delta:
        return [], 0
    anchor_recs = [seq_index[s] for s in pending_seqs if s in seq_index]
    anchor_ts = max((r.timestamp for r in anchor_recs), default=delta[-1].timestamp)
    target_uids = frozenset(str(r.user_id) for r in anchor_recs if r.role == "user" and r.user_id)

    quote_seqs: set[int] = set()
    for seq in pending_seqs:
        cur = seq
        for _ in range(_QUOTE_HOPS):
            rec = seq_index[cur] if cur in seq_index else None
            if rec is None or rec.link is None or rec.link.reply_to_seq is None:
                break
            cur = rec.link.reply_to_seq
            if cur in seq_index:
                quote_seqs.add(cur)

    keep_seqs: set[int] = set(pending_seqs) | quote_seqs
    cutoff_recent = anchor_ts - _SLICE_RECENT_WINDOW_S
    if target_uids:
        self_pre = [
            r.link.seq
            for r in delta
            if r.role == "user"
            and r.link is not None
            and r.link.seq not in keep_seqs
            and str(r.user_id or "") in target_uids
            and r.timestamp >= cutoff_recent
        ]
        keep_seqs.update(self_pre[-_SLICE_SELF_PRE_LIMIT:])
    ambient = [
        r.link.seq
        for r in delta
        if r.link is not None
        and r.link.seq not in keep_seqs
        and r.timestamp >= cutoff_recent
        and not _is_unrelated_command_noise(r, target_uids)
    ]
    keep_seqs.update(ambient[-group_recent_limit:])

    by_seq = {r.link.seq: r for r in delta if r.link is not None}
    for seq in quote_seqs:
        if seq in seq_index and seq not in by_seq:
            by_seq[seq] = seq_index[seq]
    selected = [
        r
        for seq, r in sorted(by_seq.items())
        if seq in keep_seqs
        and (seq in pending_seqs or seq in quote_seqs or anchor_ts - r.timestamp <= _SLICE_MAX_AGE_S)
    ]
    return selected, sum(1 for r in delta if r.link is not None and r.link.seq not in keep_seqs)


def _collapse_consecutive_repeats(
    records: Sequence[MessageRecord],
    lines: list[_Line],
    pending_seqs: frozenset[int],
    ctx: _RenderCtx,
) -> list[_Line]:
    """同说话人连续 ≥3 条非待回应相同正文折叠为一行，避免刷屏占窗。"""
    if len(lines) < _SPAM_REPEAT_MIN:
        return lines
    rec_by_seq = {r.link.seq: r for r in records if r.link is not None}

    def _sig(line: _Line) -> tuple[str, str] | None:
        rec = (
            rec_by_seq[line.first]
            if (len(line.seqs) == 1 and line.first not in pending_seqs and line.first in rec_by_seq)
            else None
        )
        if rec is None or rec.role != "user" or not rec.content.strip():
            return None
        return (str(rec.user_id or ""), rec.content.strip())

    out: list[_Line] = []
    i = 0
    while i < len(lines):
        sig = _sig(lines[i])
        j = i + 1
        while sig is not None and j < len(lines) and _sig(lines[j]) == sig:
            j += 1
        if sig is not None and j - i >= _SPAM_REPEAT_MIN:
            first_rec = rec_by_seq[lines[i].first]
            last_seq = lines[j - 1].last
            body = _one_line(_guard_body(first_rec.content), _MAX_ANCHOR_CHARS)
            ts = _format_timestamp(first_rec.timestamp)
            text = f"#{lines[i].first}–#{last_seq} [{ts}] {_ctx_speaker(first_rec, ctx)}: {body} (×{j - i})"
            seqs = tuple(s for ln in lines[i:j] for s in ln.seqs)
            out.append(_Line(first=lines[i].first, last=last_seq, seqs=seqs, text=text))
        else:
            out.extend(lines[i:j])
        i = j
    return out


def render_delta(
    records: Sequence[MessageRecord],
    *,
    from_seq: int,
    head_seq: int,
    backfill: bool,
    pending_seqs: frozenset[int] | tuple[int, ...],
    persona_name: str | None,
    private: bool,
    session_id: str = "",
    bot_id: str = "",
) -> str:
    """B 轨增量块：群聊按统一相关性公式切片；私聊保留线性增量。"""
    from gsuid_core.ai_core.configs.ai_config import ai_config

    linked = [r for r in records if r.link is not None]
    seq_index = {r.link.seq: r for r in linked if r.link is not None}
    pending = frozenset(pending_seqs)
    delta = [r for r in linked if r.link is not None and r.link.seq > from_seq]
    if backfill:
        anchor_ts = max(
            (seq_index[s].timestamp for s in pending if s in seq_index),
            default=(delta[-1].timestamp if delta else (linked[-1].timestamp if linked else 0.0)),
        )
        before = sorted(
            (
                r
                for r in linked
                if r.link is not None
                and r.link.seq <= from_seq
                and (private or anchor_ts <= 0.0 or anchor_ts - r.timestamp <= _SLICE_MAX_AGE_S)
            ),
            key=lambda r: r.link.seq if r.link is not None else 0,
        )
        delta = before[-_BACKFILL_LINES:] + delta

    dropped = 0
    if not private and pending:
        delta, dropped = _select_group_slice(delta, pending_seqs=pending, seq_index=seq_index)

    ctx = _RenderCtx(
        seq_index=seq_index,
        persona_name=persona_name,
        session_id=session_id or _first_meta_str(linked, "session_id"),
        bot_id=bot_id or _first_meta_str(linked, "bot_id"),
    )
    lines = _collapse_consecutive_repeats(delta, _render_lines(delta, ctx), pending, ctx)
    max_lines = int(ai_config.get_config("inbox_delta_max_lines").data)
    if len(lines) > max_lines:
        dropped += len(lines) - max_lines
        lines = lines[-max_lines:]
    rendered = [line.text for line in lines]
    if dropped > 0:
        rendered = [omitted_lines_marker(dropped), *rendered]
    first = lines[0].first if lines else from_seq
    head = thread_view_header(first, head_seq, private=private, backfill=backfill)
    return "\n".join([head, *rendered])


def render_window(
    records: Sequence[MessageRecord],
    *,
    limit: int = _BACKFILL_LINES,
    persona_name: str | None = None,
    session_id: str = "",
    bot_id: str = "",
) -> str:
    """最近 limit 条全窗口视图：heartbeat / chat_history / WebConsole 诊断用。"""
    linked = [r for r in records if r.link is not None]
    linked.sort(key=lambda r: r.link.seq if r.link is not None else 0)
    window = linked[-limit:] if limit > 0 else linked
    seq_index = {r.link.seq: r for r in linked if r.link is not None}
    ctx = _RenderCtx(
        seq_index=seq_index,
        persona_name=persona_name,
        session_id=session_id or _first_meta_str(linked, "session_id"),
        bot_id=bot_id or _first_meta_str(linked, "bot_id"),
    )
    lines = _render_lines(window, ctx)
    private = any(
        rec.role == "user" and str(rec.metadata["user_type"]) == "direct"
        for rec in window
        if "user_type" in rec.metadata
    )
    if lines:
        first, last = lines[0].first, lines[-1].last
    else:
        first = last = 0
    head = thread_view_header(first, last, private=private, backfill=False)
    return "\n".join([head, *[line.text for line in lines]])


def _last_reply_to(seq_index: dict[int, MessageRecord], uid: str) -> int | None:
    """最后一次回给 uid 的出站 seq。"""
    best: int | None = None
    for rec in seq_index.values():
        link = rec.link
        if rec.role != "assistant" or link is None:
            continue
        meta = rec.metadata
        if "reply_to_user_id" in meta and str(meta["reply_to_user_id"]) == uid:
            if best is None or link.seq > best:
                best = link.seq
    return best


def _activity_after(seq_index: dict[int, MessageRecord], seq: int, target_uid: str, speaker_uid: str) -> str:
    """seq 之后：目标说话人的回复次数 + 引用该条的他人人数。"""
    replies = 0
    quoters: set[str] = set()
    for rec in seq_index.values():
        link = rec.link
        if rec.role != "user" or link is None or link.seq <= seq:
            continue
        if str(rec.user_id) == target_uid:
            replies += 1
        if link.reply_to_seq == seq and str(rec.user_id) != speaker_uid:
            quoters.add(str(rec.user_id))
    target = _name_by_uid(seq_index, target_uid) if target_uid else ""
    answer = f"{target} 回复 {replies} 次" if replies else f"{target} 无回复"
    return f"#{seq} 之后：{answer}，另有 {len(quoters)} 人引用"


def _pending_fact(entry_item_uid: str, entry_seq: int, seq_index: dict[int, MessageRecord]) -> str:
    """待判断项行尾事实：有无往来；引用自己的显式标出引用归属与活跃度。"""
    last = _last_reply_to(seq_index, entry_item_uid)
    contact = f"你上次回他是 #{last}" if last is not None else "之前未与你往来"
    rec = seq_index[entry_seq] if entry_seq in seq_index else None
    extra = ""
    if rec is not None and rec.link is not None and rec.link.reply_to_seq is not None:
        target = seq_index[rec.link.reply_to_seq] if rec.link.reply_to_seq in seq_index else None
        if target is not None and target.role == "assistant":
            meta = target.metadata
            target_uid = (
                str(meta["reply_to_user_id"]) if "reply_to_user_id" in meta and meta["reply_to_user_id"] else ""
            )
            ago = _ago(rec.timestamp - target.timestamp)
            if _speech_channel(target) in _RECEIPT_CHANNELS:
                owner = "本人触发" if target_uid == entry_item_uid else "他人触发"
                quote_tag = f"引用对象=你[回执]·由{owner}({ago})"
            elif target_uid == entry_item_uid:
                quote_tag = f"引用对象=你→本人({ago})"
            else:
                who = _name_by_uid(seq_index, target_uid) if target_uid else "未知"
                quote_tag = f"引用对象=你→他人({who}·{ago})"
            extra = f"；{quote_tag}；{_activity_after(seq_index, rec.link.reply_to_seq, target_uid, entry_item_uid)}"
    return f"{contact}{extra}"


def format_pending_entry_line(
    entry: PendingEntry,
    seq_index: dict[int, MessageRecord],
    ctx: _RenderCtx,
    *,
    include_kind: bool = False,
) -> str:
    """渲染单条待回应项行（主 Agent 待回应块与 TurnRouter 共享）。"""
    item = entry.item
    if entry.receipt is not None:
        plugin = entry.receipt.plugin_label or "插件"
        body = _one_line(entry.receipt.receipt_text or "[图片]", 100)
        who = _name_by_uid(seq_index, item.user_id)
        prefix = f"★#{item.seq} receipt · " if include_kind else f"★#{item.seq} [命令回执] "
        return f"{prefix}插件={plugin} · 触发者={who}「{body}」"
    prefix = f"★#{item.seq} {item.kind} · " if include_kind else f"★#{item.seq} "
    rec = seq_index[item.seq] if item.seq in seq_index else None
    if rec is not None:
        markers = _at_markers(rec, seq_index) + _quote_marker(rec, seq_index, ctx.persona_name)
        body = _one_line(_guard_body(rec.content), _MAX_ANCHOR_CHARS)
        head = f"{prefix}{_ctx_speaker(rec, ctx)}{markers}「{body}」"
    else:
        head = f"{prefix}用户ID:{item.user_id}"
    return f"{head} · {_pending_fact(item.user_id, item.seq, seq_index)}"


def render_router_view(
    turn: InboxTurn,
    records: Sequence[MessageRecord],
    *,
    relation_label: str = "",
    group_rate_1m: int = 0,
) -> str:
    """精简线程视图 + 待判断列表 + 场景元信息，给 TurnRouter 读。"""
    linked = [r for r in records if r.link is not None]
    linked.sort(key=lambda r: r.link.seq if r.link is not None else 0)
    seq_index = {r.link.seq: r for r in linked if r.link is not None}
    pending = frozenset(e.item.seq for e in turn.entries)
    ctx = _RenderCtx(
        seq_index=seq_index,
        persona_name=turn.persona_name,
        session_id=turn.session_id or _first_meta_str(linked, "session_id"),
        bot_id=str(turn.primary.event.bot_id or "") or _first_meta_str(linked, "bot_id"),
        compact=True,
    )
    tail, _ = _select_group_slice(
        linked,
        pending_seqs=pending,
        seq_index=seq_index,
        group_recent_limit=_ROUTER_RECENT_LIMIT,
    )
    # 补入你上次回该说话人的最后一条（30 分钟内），便于判定 follow_self 续聊关系
    last_reply_seq = _last_reply_to(seq_index, turn.primary.item.user_id)
    if last_reply_seq is not None and last_reply_seq in seq_index:
        tail_seqs = {r.link.seq for r in tail if r.link is not None}
        if last_reply_seq not in tail_seqs:
            anchor_ts = max(
                (seq_index[s].timestamp for s in pending if s in seq_index),
                default=(linked[-1].timestamp if linked else 0.0),
            )
            last_rec = seq_index[last_reply_seq]
            if anchor_ts <= 0.0 or anchor_ts - last_rec.timestamp <= _SLICE_MAX_AGE_S:
                tail = sorted([*tail, last_rec], key=lambda r: r.link.seq if r.link is not None else 0)

    lines = _collapse_consecutive_repeats(tail, _render_lines(tail, ctx), pending, ctx)
    out = [
        ROUTER_TIMELINE_HEAD,
        *(line.text for line in lines),
        ROUTER_PENDING_HEAD,
        *(format_pending_entry_line(e, seq_index, ctx, include_kind=True) for e in turn.entries),
    ]
    if relation_label or group_rate_1m > 0:
        scene_parts: list[str] = []
        if relation_label:
            scene_parts.append(f"关系={relation_label}")
        if group_rate_1m > 0:
            scene_parts.append(f"近1分钟群消息={group_rate_1m}条")
        scene_parts.append(f"触发={turn.primary.item.kind}")
        out.append("[场景] " + " · ".join(scene_parts))
    out.append(ROUTER_PRINCIPLE)
    return "\n".join(out)
