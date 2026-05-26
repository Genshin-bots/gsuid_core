"""AI Core 数据库模块 —— 好感度等基础数据模型

把 ai_core 的**基础**关系型表收在一处。仅放"框架级用户/群组维度的轻量
状态"——记忆 / 任务 / 状态等大表分别在 ``memory/database`` /
``planning/models`` / ``state_store/models`` 自治。

模块组成：
- ``models.py`` : SQLAlchemy 模型（``UserFavorability`` 等）

注：所有 ai_core 表共享 ``gsuid_core.utils.database.startup`` 的初始化入口，
新表加在自身子模块即可，无需改本文件。
"""

from gsuid_core.ai_core.database.models import UserFavorability

__all__ = [
    "UserFavorability",
]
