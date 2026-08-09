"""用户回合（User Turn）归属 contextvar。

主人格交互 root run 生成 ``user_turn_id`` 后写入本 ContextVar，
同 task 内同步嵌套的 subagent / capability agent / 图片理解等自动继承，
便于 settle 时把 token 归入同一用户回合树。

异步后台任务（Kanban kick 另开 task 且未显式绑定）不保证继承——不计入用户回合，
避免把延后执行的成本算进「一次用户对话」。
"""

from __future__ import annotations

import contextvars
from typing import Optional

# 当前用户回合 id；None 表示不在任何用户回合树内（后台/巡检等）
_current_user_turn_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("gs_user_turn_id", default=None)


def get_user_turn_id() -> Optional[str]:
    """读取当前 task 上的用户回合 id（无则 None）。"""
    return _current_user_turn_id.get()


def set_user_turn_id(turn_id: Optional[str]) -> contextvars.Token:
    """绑定用户回合；结束时须 ``reset_user_turn_id``。"""
    return _current_user_turn_id.set(turn_id)


def reset_user_turn_id(token: contextvars.Token) -> None:
    """还原 ``set_user_turn_id``。"""
    _current_user_turn_id.reset(token)
