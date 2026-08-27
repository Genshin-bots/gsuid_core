"""评测前清副作用：取消 eval_ 定时任务，清空 eval_ 记忆与好感。

不碰公共 RAG / 生产记忆。评测 user_id / group_id 带 eval_ 前缀。
"""

from __future__ import annotations

from sqlmodel import col
from sqlalchemy import or_, delete, update
from sqlalchemy.engine import CursorResult

from gsuid_core.utils.database import base_models
from gsuid_core.ai_core.database.models import UserFavorability
from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

_EVAL_MEM_PREFIXES = (
    "group:eval_grp_",
    "user_global:eval_",
    "user_in_group:eval_",
)


async def reset_eval_side_effects() -> dict[str, int]:
    """取消评测定时任务并清空评测记忆/好感。返回计数。"""
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
        n_sched = result.rowcount if isinstance(result, CursorResult) else 0
        fav_stmt = delete(UserFavorability).where(col(UserFavorability.user_id).like("eval_%"))
        fav_result = await session.execute(fav_stmt)
        n_fav = fav_result.rowcount if isinstance(fav_result, CursorResult) else 0
        await session.commit()

    # clear_ops 在 import 时绑定 async_maker；须在 init_database 之后再导入。
    from gsuid_core.ai_core.memory.database.clear_ops import clear_memories_for_scope_async

    n_ep = 0
    n_scope = 0
    for prefix in _EVAL_MEM_PREFIXES:
        cleared = await clear_memories_for_scope_async(scope_pattern=prefix)
        raw = cleared["data"] if "data" in cleared else None
        if not isinstance(raw, dict):
            continue
        scopes = raw["affected_scope_keys"] if "affected_scope_keys" in raw else []
        n_scope += len(scopes) if isinstance(scopes, list) else 0
        if "deleted_episodes" in raw:
            n_ep += int(raw["deleted_episodes"])
    return {
        "cancelled_schedules": n_sched,
        "cleared_favorability": n_fav,
        "cleared_memory_scopes": n_scope,
        "deleted_episodes": n_ep,
    }
