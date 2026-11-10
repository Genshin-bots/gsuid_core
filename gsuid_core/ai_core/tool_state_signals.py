"""状态实体信号与能力族边界定义。"""

# 有持久实体才该进池的族：核内种子不闭合同族（避免闲聊/旁观提前挂上变更工具）。
STATE_DRIVEN_FAMILY_DOMAINS: frozenset[str] = frozenset({"长期任务编排", "产物", "定时任务", "结构化记录"})


async def user_has_active_schedules(user_id: str) -> bool:
    """用户是否存在未完成(pending/paused)的定时任务。"""
    from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

    rows = await AIScheduledTask.select_rows(user_id=user_id)
    for row in rows or []:
        task = row if isinstance(row, AIScheduledTask) else AIScheduledTask(**row)
        if task.status in ("pending", "paused"):
            return True
    return False
