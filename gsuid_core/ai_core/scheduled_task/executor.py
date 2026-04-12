"""
execute_scheduled_task 定时执行器

当 APScheduler 触发定时任务时，调用此函数执行。
使用 get_ai_session(event) 加载当时的 persona 和 session 来执行任务，
保持回复语气与主 Agent 一致。
"""

from typing import Optional
from datetime import datetime

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event

from .models import AIScheduledTask


async def execute_scheduled_task(task_id: str) -> None:
    """
    执行定时 AI 任务

    这是被 APScheduler 调用的统一执行器。当时间到达时，
    此函数会：
    1. 从数据库读取任务信息
    2. 构建 Event 对象
    3. 使用 get_ai_session(event) 加载当时的 persona/session
    4. 向 session 发送任务消息，让 AI 执行
    5. 将结果推送给用户

    Args:
        task_id: 任务ID，从 APScheduler 传递
    """
    from gsuid_core.gss import gss

    logger.info(f"⏰ [ScheduledTask] 开始执行定时任务: {task_id}")

    # 1. 从数据库读取任务信息
    tasks = await AIScheduledTask.select_rows(task_id=task_id)

    if not tasks:
        logger.error(f"❌ [ScheduledTask] 任务不存在: {task_id}")
        return

    task = tasks[0]
    if not isinstance(task, AIScheduledTask):
        task = AIScheduledTask(**task)

    if task.status != "pending":
        logger.warning(f"⚠️ [ScheduledTask] 任务状态非 pending，跳过执行: task_id={task_id}, status={task.status}")
        return

    # 2. 构建 Event 对象
    ev = Event(
        bot_id=task.bot_id,
        user_id=task.user_id,
        bot_self_id=task.bot_self_id,
        user_type=task.user_type,  # type: ignore
        group_id=task.group_id,
        real_bot_id=task.bot_id,
        msg_id="",
    )

    # 3. 获取 Bot 实例用于发送消息
    bot_instance: Optional[Bot] = None
    if task.WS_BOT_ID:
        if task.WS_BOT_ID in gss.active_bot:
            BOT = gss.active_bot[task.WS_BOT_ID]
            bot_instance = Bot(BOT, ev)
        else:
            logger.error(f"[ScheduledTask] 机器人{task.WS_BOT_ID}不存在!")
    else:
        for bot_id in gss.active_bot:
            BOT = gss.active_bot[bot_id]
            bot_instance = Bot(BOT, ev)
            break  # 只使用第一个

    # 4. 使用 get_ai_session 加载 persona 和 session
    try:
        from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
        from gsuid_core.ai_core.ai_router import get_ai_session
        from gsuid_core.ai_core.statistics.manager import statistics_manager

        logger.info(
            f"🧠 [ScheduledTask] 加载 session 执行任务: session_id={task.session_id}, persona={task.persona_name}"
        )

        # 记录触发方式为 scheduled
        statistics_manager.record_trigger(trigger_type="scheduled")

        # 获取 AI session（会自动加载 persona）
        session: GsCoreAIAgent = await get_ai_session(ev)

        # 构建任务消息
        task_message = (
            f"【定时任务执行】请完成以下任务，直接输出结果（不要有多余的解释）：\n\n任务内容：{task.task_prompt}"
        )

        # 通过 session 执行任务
        result = await session.run(
            user_message=task_message,
            bot=bot_instance,
            ev=ev,
        )

        # 5. 更新任务状态和结果
        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data={
                "status": "executed",
                "executed_at": datetime.now(),
                "result": result,
            },
        )

        # 6. 推送结果给用户
        if bot_instance:
            if result:
                await bot_instance.send(result)
            logger.info(f"✅ [ScheduledTask] 任务执行成功并已推送: task_id={task_id}")
        else:
            logger.warning(f"⚠️ [ScheduledTask] 无法获取 Bot 实例，结果未推送: task_id={task_id}")

    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 任务执行失败: {task_id}, error={e}")

        # 更新任务状态为失败
        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data={
                "status": "failed",
                "executed_at": datetime.now(),
                "error_message": str(e),
            },
        )


async def reload_pending_tasks() -> int:
    """
    重新加载所有待执行的定时任务到 APScheduler

    在系统启动时调用，确保重启前的待执行任务仍然有效。

    Returns:
        加载的任务数量
    """
    from gsuid_core.aps import scheduler

    # 查询所有 pending 状态的任务
    tasks = await AIScheduledTask.select_rows(status="pending")

    count = 0
    for task_data in tasks:
        task = AIScheduledTask(**task_data)

        # 检查触发时间是否已过
        if task.trigger_time <= datetime.now():
            # 立即执行
            logger.info(f"⏰ [ScheduledTask] 发现已到期的任务，立即执行: {task.task_id}")
            await execute_scheduled_task(task.task_id)
        else:
            # 重新注册到调度器
            scheduler.add_job(
                func=execute_scheduled_task,
                trigger="date",
                run_date=task.trigger_time,
                args=[task.task_id],
                id=task.task_id,
                replace_existing=True,
            )
            count += 1
            logger.info(f"📋 [ScheduledTask] 重新加载待执行任务: {task.task_id}, 触发时间: {task.trigger_time}")

    logger.info(f"✅ [ScheduledTask] 共重新加载 {count} 个待执行任务")
    return count
