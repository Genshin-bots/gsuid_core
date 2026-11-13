"""回执唤醒项的上下文渲染 + 工具策略 + 落定后建议入库。

``command_aside`` 在提交时把插件标签 / 建议记忆 / 工具开关塞进 ``ReceiptContext``；
本模块负责在 ``inbox_pending`` 块里把这些上下文摆到该项下面，并在回合运行期
绑定工具白名单、在落定后把「失败/用法类回执」的人格点评存回建议记忆。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Sequence

from gsuid_core.message_history import MessageRecord

if TYPE_CHECKING:
    from pydantic_ai import Tool

    from gsuid_core.ai_core.models import ToolContext
    from gsuid_core.ai_core.inbox.turn import InboxTurn


def receipt_context_lines(turn: "InboxTurn", records: Sequence[MessageRecord]) -> str:
    """回执项上下文块：插件标签、已有建议记忆、allow_tools 时的源码摘录。"""
    from gsuid_core.ai_core.control.lines import RECEIPT_CONTEXT_HEAD

    out: list[str] = []
    for entry in turn.entries:
        ctx = entry.receipt
        if ctx is None:
            continue
        if not out:
            out.append(RECEIPT_CONTEXT_HEAD)
        label = ctx.plugin_label or (ctx.plugin_root.name if ctx.plugin_root is not None else "")
        out.append(f"· #{entry.item.seq} 回执（{label or '未知插件'}）：")
        if ctx.advice_memory:
            out.append(f"  已有建议记忆：{ctx.advice_memory}")
        if ctx.allow_tools and ctx.plugin_root is not None and not ctx.advice_memory:
            from gsuid_core.ai_core.command_aside.source_lookup import find_receipt_excerpts

            excerpts = find_receipt_excerpts(ctx.plugin_root, ctx.receipt_text, ctx.entry_rel)
            for rel, excerpt in excerpts:
                out.append(f"  源码摘录 {rel}：{excerpt}")
    return "\n".join(out)


def receipt_tool_policy(turn: "InboxTurn") -> tuple[bool, tuple[Tool[ToolContext], ...]]:
    """回合工具策略：回执批的工具放行与否 + 插件源码阅读工具挂载。"""
    extra: list[Tool[ToolContext]] = []
    allow = False
    for entry in turn.entries:
        ctx = entry.receipt
        if ctx is None:
            continue
        if ctx.allow_tools and not ctx.advice_memory:
            allow = True
            if ctx.plugin_root is not None:
                from gsuid_core.ai_core.command_aside.plugin_read import make_plugin_source_tool

                extra.append(make_plugin_source_tool(ctx.plugin_root))
    return allow, tuple(extra)


def settle_receipt_entries(turn: "InboxTurn", advice: str, persona_name: str) -> None:
    """回执项被回答且回执像失败/用法 → 存建议记忆；实际作答即记点评冷却。"""
    from gsuid_core.ai_core.angle_bracket_guard import strip_protocol_tags
    from gsuid_core.ai_core.command_aside.recall import save_advice
    from gsuid_core.ai_core.command_aside.runner import _last_aside_comment_ts, _is_failure_or_usage_receipt

    advice = strip_protocol_tags(advice).strip()
    commented = False
    for entry in turn.entries:
        ctx = entry.receipt
        if ctx is None or entry.item.seq not in turn.answered:
            continue
        commented = commented or bool(advice)
        plugin = ctx.plugin_root.name if ctx.plugin_root is not None else ""
        if advice and not ctx.advice_memory and plugin and _is_failure_or_usage_receipt(ctx.receipt_text):
            save_advice(plugin, ctx.receipt_text, advice, trigger_id=ctx.entry_rel, persona_name=persona_name)
    if commented:
        _last_aside_comment_ts[turn.session_id] = time.time()
