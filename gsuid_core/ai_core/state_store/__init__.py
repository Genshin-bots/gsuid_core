"""
通用持久状态存储模块

框架级别的键值存储能力，让复杂任务的结构化状态跨会话存活。
任何插件和任务场景都可以通过 state_* 工具读写持久数据。

- models.py: AIPersistentState 数据库模型
- store.py:  set / get / delete / list / append 核心读写逻辑
- tools.py:  暴露给 Agent 的 state_* 工具
"""

from .store import (
    state_mutate,
    state_get_value,
    state_list_keys,
    state_set_value,
    state_append_item,
    state_delete_value,
)
from .tools import (
    state_get,
    state_set,
    state_list,
    state_append,
    state_delete,
)
from .models import AIPersistentState

__all__ = [
    "AIPersistentState",
    "state_mutate",
    "state_get_value",
    "state_set_value",
    "state_delete_value",
    "state_list_keys",
    "state_append_item",
    "state_get",
    "state_set",
    "state_delete",
    "state_list",
    "state_append",
]
