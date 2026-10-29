"""统一审批中心：一张表（AIApprovalRequest）+ 两个动词（submit / resolve）。

裁决入口是私聊确认码和 webconsole（含 Kanban 兼容端点）。带确认码的新票
批准后回灌调用会话；没有哈希的旧票仍走原来的领域回调。
"""

from .center import (
    CONSOLE_RESOLVER,
    locate,
    submit,
    resolve,
    is_master,
    expire_due,
    has_pending,
    resolve_row,
    submit_meta,
    expire_stale,
    log_question,
    prime_pending,
    is_full_access,
    tool_call_gate,
    grant_tool_call,
    set_full_access,
    consume_tool_grant,
    note_submit_reused,
    match_confirm_message,
    set_full_access_resolver,
    list_pending_for_resolver,
    register_approval_category,
    plugin_install_block_reason,
    register_builtin_categories,
)
from .models import AIApprovalRequest

__all__ = [
    "AIApprovalRequest",
    "CONSOLE_RESOLVER",
    "submit",
    "resolve",
    "resolve_row",
    "locate",
    "list_pending_for_resolver",
    "expire_stale",
    "expire_due",
    "log_question",
    "prime_pending",
    "has_pending",
    "is_master",
    "submit_meta",
    "note_submit_reused",
    "set_full_access",
    "set_full_access_resolver",
    "is_full_access",
    "tool_call_gate",
    "grant_tool_call",
    "consume_tool_grant",
    "match_confirm_message",
    "plugin_install_block_reason",
    "register_approval_category",
    "register_builtin_categories",
]
