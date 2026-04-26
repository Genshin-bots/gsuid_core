from typing import Tuple

from gsuid_core.models import Event


def check_pm(ev: Event) -> Tuple[bool, str]:
    """检查用户是否为管理员.

    Args:
        ev: Event 实例,包含事件相关信息

    Returns:
        如果用户是管理员,返回 True;否则返回 False
    """
    if ev.user_pm == 0:
        return True, "✅ 您是管理员，为你进行操作！"
    return False, "🚫 您不是管理员，权限不足，无法执行此操作！"
