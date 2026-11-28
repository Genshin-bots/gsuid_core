"""收件箱当轮动态块：待你回应与窗口外引用事实。

入史前由 _peel_ephemeral_system_lines 整块剥除；块头字面量只在 control/lines.py。
"""

from __future__ import annotations

from typing import Sequence

from gsuid_core.message_history import MessageRecord
from gsuid_core.ai_core.inbox.turn import InboxTurn
from gsuid_core.ai_core.thread_view import (
    _RenderCtx,
    _replies_around,
    format_pending_entry_line,
)
from gsuid_core.ai_core.control.lines import (
    PENDING_BLOCK_HEAD,
    pending_later_delivery_note,
)

__all__ = ["render_pending_block", "quote_fact_lines"]


def _index(records: Sequence[MessageRecord]) -> dict[int, MessageRecord]:
    return {r.link.seq: r for r in records if r.link is not None}


def render_pending_block(
    turn: InboxTurn,
    records: Sequence[MessageRecord],
    extra_lines: Sequence[str] = (),
) -> str:
    """（待你回应）补充块：正文已在时间线标 ★#seq，仅补命令回执、滞后送达与出窗引用事实。"""
    seq_index = _index(records)
    ctx = _RenderCtx(seq_index=seq_index, persona_name=turn.persona_name)
    lines: list[str] = []
    for entry in turn.entries:
        if entry.receipt is not None:
            lines.append(format_pending_entry_line(entry, seq_index, ctx))
            continue
        _, later = _replies_around(seq_index, entry.item.user_id, entry.item.seq)
        if later is not None:
            lines.append(f"★#{entry.item.seq} {pending_later_delivery_note(entry.item.seq, later)}")
    lines.extend(extra_lines)
    if not lines:
        return ""
    return "\n".join([PENDING_BLOCK_HEAD, *lines])


async def quote_fact_lines(turn: InboxTurn, records: Sequence[MessageRecord]) -> list[str]:
    """待回应项引用窗口外消息时，走 OutboundAudit 补一条事实行（无指令措辞）。"""
    from gsuid_core.ai_core.outbound import resolve_quote

    seq_index = _index(records)
    out: list[str] = []
    for entry in turn.entries:
        ev = entry.event
        if not (ev.reply_id or ev.reply):
            continue
        rec = seq_index[entry.item.seq] if entry.item.seq in seq_index else None
        link = rec.link if rec is not None else None
        if link is not None and link.reply_to_seq is not None and link.reply_to_seq in seq_index:
            continue
        hit = await resolve_quote(ev)
        if hit is not None and hit.line:
            out.append(f"★#{entry.item.seq} {hit.line}")
    return out
