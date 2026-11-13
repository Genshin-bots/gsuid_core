"""收件箱当轮动态块：待你回应 / 工具身份 / 相关往来。

入史前由 _peel_ephemeral_system_lines 整块剥除；块头字面量只在 control/lines.py。
H1 只做结构事实渲染；回执建议记忆 / 源码摘录等 DB 查询由 H2 补。
"""

from __future__ import annotations

from typing import Sequence
from dataclasses import dataclass

from gsuid_core.message_history import MessageRecord
from gsuid_core.ai_core.inbox.turn import InboxTurn
from gsuid_core.ai_core.thread_view import (
    _MAX_ANCHOR_CHARS,
    _one_line,
    _RenderCtx,
    _at_markers,
    _guard_body,
    _pending_fact,
    _quote_marker,
    _speaker_label,
)
from gsuid_core.ai_core.control.lines import (
    PENDING_BLOCK_HEAD,
    RELATED_BLOCK_HEAD,
    IDENTITY_BLOCK_HEAD,
)

__all__ = [
    "render_pending_block",
    "render_identity_block",
    "render_related_block",
    "quote_fact_lines",
]


def _index(records: Sequence[MessageRecord]) -> dict[int, MessageRecord]:
    return {r.link.seq: r for r in records if r.link is not None}


def _last_exchange(seq_index: dict[int, MessageRecord], uid: str) -> tuple[int | None, int | None]:
    """该说话人最近一次被 Bot 回复的 (in_seq, out_seq)。"""
    best: tuple[int, int] | None = None
    for rec in seq_index.values():
        link = rec.link
        if rec.role != "assistant" or link is None or not link.in_reply_to_seqs:
            continue
        meta = rec.metadata
        if "reply_to_user_id" in meta and str(meta["reply_to_user_id"]) == uid:
            pair = (link.in_reply_to_seqs[0], link.seq)
            if best is None or pair[1] > best[1]:
                best = pair
    if best is None:
        return None, None
    return best


def _speaker_continued(seq_index: dict[int, MessageRecord], out_seq: int, uid: str) -> int | None:
    """出站 out_seq 之后该说话人的第一条发言 seq。"""
    for rec in sorted(seq_index.values(), key=lambda r: r.link.seq if r.link else 0):
        if rec.role != "user" or rec.link is None or rec.link.seq <= out_seq:
            continue
        if str(rec.user_id) == uid:
            return rec.link.seq
    return None


def render_pending_block(turn: InboxTurn, records: Sequence[MessageRecord]) -> str:
    """（待你回应）块：每条唤醒项一行，行尾给可核实的往来事实。"""
    seq_index = _index(records)
    ctx = _RenderCtx(seq_index=seq_index, persona_name=turn.persona_name)
    uids = {e.item.user_id for e in turn.entries}
    head = f"{PENDING_BLOCK_HEAD}本批 {len(uids)} 人"
    out = [head]
    for entry in turn.entries:
        item = entry.item
        rec = seq_index[item.seq] if item.seq in seq_index else None
        if rec is not None:
            speaker = _speaker_label(rec)
            markers = _at_markers(rec, seq_index) + _quote_marker(rec, seq_index, ctx.persona_name)
            body = _one_line(_guard_body(rec.content), _MAX_ANCHOR_CHARS)
            line = f"★#{item.seq} {speaker}{markers}「{body}」"
        else:
            line = f"★#{item.seq} 用户ID:{item.user_id}"
        out.append(f"{line} · {_pending_fact(item.user_id, item.seq, seq_index)}")
    return "\n".join(out)


async def quote_fact_lines(turn: InboxTurn, records: Sequence[MessageRecord]) -> list[str]:
    """待回应项引用窗口外消息时，走 OutboundAudit 补一条事实行（无指令措辞）。"""
    from gsuid_core.ai_core.outbound import resolve_quote

    seq_index = _index(records)
    out: list[str] = []
    for entry in turn.entries:
        rec = seq_index[entry.item.seq] if entry.item.seq in seq_index else None
        link = rec.link if rec is not None else None
        if link is None or link.reply_to_seq is None or link.reply_to_seq in seq_index:
            continue
        hit = await resolve_quote(entry.event)
        if hit is not None and hit.line:
            out.append(f"★#{entry.item.seq} {hit.line}")
    return out


def render_identity_block(turn: InboxTurn, records: Sequence[MessageRecord]) -> str:
    """（工具身份）块：只在本批多人时出现。"""
    _ = records
    if not turn.multi_target:
        return ""
    primary = turn.primary.item.seq
    example = next((e.item.seq for e in turn.entries if e.item.seq != primary), primary)
    return (
        f'{IDENTITY_BLOCK_HEAD}本批有多人：调用非只读工具前必须先调 act_for("#{primary}")'
        f' 或 act_for("#{example}") 声明替谁执行；换人再调一次。'
    )


@dataclass(frozen=True, slots=True)
class _RelatedRow:
    seq: int
    text: str


def render_related_block(turn: InboxTurn, records: Sequence[MessageRecord]) -> str:
    """（相关往来）块：每个待回应说话人一行最近往来事实。"""
    seq_index = _index(records)
    out = [RELATED_BLOCK_HEAD]
    seen: set[str] = set()
    for entry in turn.entries:
        uid = entry.item.user_id
        if uid in seen:
            continue
        seen.add(uid)
        rec = seq_index[entry.item.seq] if entry.item.seq in seq_index else None
        if rec is not None and rec.user_name:
            speaker = rec.user_name
        else:
            speaker = f"用户ID:{uid}"
        in_seq, out_seq = _last_exchange(seq_index, uid)
        if in_seq is None:
            out.append(f"· #{entry.item.seq}：你与{speaker} 无往来记录")
            continue
        nxt = _speaker_continued(seq_index, out_seq or 0, uid)
        if nxt == entry.item.seq:
            tail = f"#{out_seq} 之后{speaker} 的下一句就是本条"
        elif nxt is None:
            tail = f"之后{speaker} 无其他发言"
        else:
            tail = f"之后{speaker} 又说过话（最近 #{nxt}）"
        out.append(f"· #{entry.item.seq}：你与{speaker} 最近往来 #{in_seq} → #{out_seq}；{tail}")
    return "\n".join(out) if len(out) > 1 else ""
