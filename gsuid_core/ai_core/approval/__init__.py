"""统一审批中心：一张表（AIApprovalRequest）+ 两个动词（submit / resolve）。

三个裁决入口（对话工具 respond_approval / webconsole /api/ai/approvals / Kanban
看板兼容端点）+ 一个统一转达工具，全部经本模块；领域动作由 category 回调承担：

- ``command_exec``   : 执行入库 argv 快照（command_exec/approval.py 注册）
- ``kanban_subtask`` : 子任务回 pending + kick_root（planning/startup.py 注册，
                       插件安装审批也走这条——它就是 Kanban 子任务审批）
- ``tool_call``      : @ai_tools(approval=...) 策略门的一次性放行 grant（内置）
- ``agent_request``  : Agent 主动请求（request_user/master_approval，内置）
"""

from .center import (
    CONSOLE_RESOLVER,
    locate,
    submit,
    resolve,
    is_master,
    has_pending,
    resolve_row,
    expire_stale,
    log_question,
    prime_pending,
    is_full_access,
    tool_call_gate,
    grant_tool_call,
    set_full_access,
    consume_tool_grant,
    set_full_access_resolver,
    list_pending_for_resolver,
    register_approval_category,
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
    "log_question",
    "prime_pending",
    "has_pending",
    "is_master",
    "set_full_access",
    "set_full_access_resolver",
    "is_full_access",
    "tool_call_gate",
    "grant_tool_call",
    "consume_tool_grant",
    "register_approval_category",
    "register_builtin_categories",
]
