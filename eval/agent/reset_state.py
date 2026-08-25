"""评测前清副作用：取消 pending/paused 及 eval_ 前缀定时任务。

不碰公共 RAG / 生产记忆。评测 user_id 本就唯一，记忆串味主要来自 cognition 公共库；
那份不能在评测脚本里整库删。
"""

from __future__ import annotations

from sqlmodel import col
from sqlalchemy import or_, update
from sqlalchemy.engine import CursorResult

from gsuid_core.utils.database import base_models
from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask


async def reset_eval_side_effects() -> dict[str, int]:
    """取消会干扰下一趟评测的定时任务。返回 {cancelled: n}。"""
    await base_models.init_database()
    async with base_models.async_maker() as session:
        stmt = (
            update(AIScheduledTask)
            .where(
                or_(
                    col(AIScheduledTask.status).in_(("pending", "paused")),
                    col(AIScheduledTask.user_id).like("eval_%"),
                )
            )
            .values(status="cancelled")
        )
        result = await session.execute(stmt)
        n = result.rowcount if isinstance(result, CursorResult) else 0
        await session.commit()
    return {"cancelled_schedules": n}
