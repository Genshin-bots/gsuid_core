"""预算归属 scope 的 contextvar（无 gs_agent 依赖，供 agent_run 各阶段使用）。"""

from __future__ import annotations

import contextvars
from typing import Tuple, Optional

from gsuid_core.models import Event

BudgetScope = Tuple[str, str, str]

# 父 run 写入后，嵌套 LLM（含 create_task 复制的 Context）自动继承记账归属
_current_budget_scope: contextvars.ContextVar[Optional[BudgetScope]] = contextvars.ContextVar(
    "gs_budget_scope", default=None
)


def budget_scope_from_event(ev: Event) -> BudgetScope:
    """从 Event 取预算 scope 三元组 (group_id, user_id, bot_id)。私聊 group_id 为空串。"""
    return (str(ev.group_id) if ev.group_id else "", str(ev.user_id), ev.bot_id or "")


def set_budget_scope_context(scope: Optional[BudgetScope]) -> contextvars.Token:
    """为后台自主 LLM 调用设置当前预算归属 scope；结束时须 reset。"""
    return _current_budget_scope.set(scope)


def reset_budget_scope_context(token: contextvars.Token) -> None:
    """还原 set_budget_scope_context 设置的 contextvar。"""
    _current_budget_scope.reset(token)
