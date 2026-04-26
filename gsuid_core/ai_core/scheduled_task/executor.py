"""
execute_scheduled_task 定时执行器

当 APScheduler 触发定时任务时，调用此函数执行。
使用 get_ai_session(event) 加载当时的 persona 和 session 来执行任务，
保持回复语气与主 Agent 一致。

支持两种任务类型：
- once: 一次性任务，执行后状态变为 executed
- interval: 循环任务，执行后检查是否达到最大执行次数，若未达到则更新下次执行时间
"""

from typing import Optional
from datetime import datetime, timedelta

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event

from .models import AIScheduledTask

# 安全限制：最大循环执行次数
MAX_EXECUTION_LIMIT = 10


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
    6. 对于循环任务，更新下次执行时间或标记为已完成

    Args:
        task_id: 任务ID，从 APScheduler 传递
    """
    from gsuid_core.aps import scheduler
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

    if task.status not in ("pending", "paused"):
        logger.warning(
            f"⚠️ [ScheduledTask] 任务状态非 pending/paused，跳过执行: task_id={task_id}, status={task.status}"
        )
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

        # 5. 根据任务类型处理
        if task.task_type == "interval":
            # 循环任务处理
            current_exec = (task.current_executions or 0) + 1
            max_exec = task.max_executions or MAX_EXECUTION_LIMIT

            if current_exec >= max_exec:
                # 达到最大执行次数，任务结束
                # 注意：不在这里移除 APScheduler job，而是通过 shutdown 时统一清理
                # 这样可以避免因异步操作导致的 job 遗漏清理问题

                await AIScheduledTask.update_data_by_data(
                    select_data={"task_id": task_id},
                    update_data={
                        "status": "executed",
                        "executed_at": datetime.now(),
                        "current_executions": current_exec,
                        "result": result,
                    },
                )
                logger.info(
                    f"✅ [ScheduledTask] 循环任务执行完毕（已达最大次数）: task_id={task_id}, 执行了 {current_exec} 次"
                )
            else:
                # 更新下次执行时间
                interval_sec = task.interval_seconds or 0
                next_run = datetime.now() + timedelta(seconds=interval_sec)

                # 如果任务处于 paused 状态，不要重新注册
                if task.status == "pending":
                    # 重新注册到调度器
                    if scheduler.get_job(task_id):
                        scheduler.remove_job(task_id)

                    scheduler.add_job(
                        func=execute_scheduled_task,
                        trigger="interval",
                        seconds=interval_sec,
                        start_date=datetime.now(),
                        args=[task_id],
                        id=task_id,
                        replace_existing=True,
                    )

                await AIScheduledTask.update_data_by_data(
                    select_data={"task_id": task_id},
                    update_data={
                        "current_executions": current_exec,
                        "next_run_time": next_run,
                        "result": result,
                    },
                )
                logger.info(
                    f"🔄 [ScheduledTask] 循环任务执行成功: task_id={task_id}, "
                    f"第 {current_exec}/{max_exec} 次, 下次执行: {next_run}"
                )
        else:
            # 一次性任务
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
        task = task_data if isinstance(task_data, AIScheduledTask) else AIScheduledTask(**task_data)

        # 根据任务类型处理
        if task.task_type == "interval":
            # 循环任务
            if task.next_run_time and task.next_run_time > datetime.now():
                # 重新注册到调度器
                interval_sec = task.interval_seconds or 0
                scheduler.add_job(
                    func=execute_scheduled_task,
                    trigger="interval",
                    seconds=interval_sec,
                    start_date=task.next_run_time,
                    args=[task.task_id],
                    id=task.task_id,
                    replace_existing=True,
                )
                count += 1
                logger.info(f"📋 [ScheduledTask] 重新加载循环任务: {task.task_id}, 下次执行: {task.next_run_time}")
            else:
                # 已到执行时间或未设置，先执行一次
                logger.info(f"⏰ [ScheduledTask] 发现已到期的循环任务，立即执行: {task.task_id}")
                await execute_scheduled_task(task.task_id)

        else:
            # 一次性任务
            if task.trigger_time and task.trigger_time <= datetime.now():
                # 立即执行
                logger.info(f"⏰ [ScheduledTask] 发现已到期的一次性任务，立即执行: {task.task_id}")
                await execute_scheduled_task(task.task_id)
            elif task.trigger_time:
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
                logger.info(f"📋 [ScheduledTask] 重新加载一次性任务: {task.task_id}, 触发时间: {task.trigger_time}")

    logger.info(f"✅ [ScheduledTask] 共重新加载 {count} 个待执行任务")
    return count


async def cleanup_completed_tasks() -> int:
    """
    清理所有已完成任务的 APScheduler job

    在系统关闭前调用，移除所有非 pending 状态任务的 APScheduler job，
    避免重启后重复触发已完成的任务。

    Returns:
        清理的任务数量
    """
    from gsuid_core.aps import scheduler

    # 查询所有非 pending 状态的任务
    all_tasks = await AIScheduledTask.select_rows()

    cleaned_count = 0
    for task_data in all_tasks:
        task = task_data if isinstance(task_data, AIScheduledTask) else AIScheduledTask(**task_data)

        # 只处理非 pending 状态的任务
        if task.status != "pending":
            if scheduler.get_job(task.task_id):
                scheduler.remove_job(task.task_id)
                cleaned_count += 1
                logger.info(f"🧹 [ScheduledTask] 清理已完成任务: {task.task_id}, status={task.status}")

    logger.info(f"✅ [ScheduledTask] 共清理 {cleaned_count} 个已完成任务的 APScheduler job")
    return cleaned_count
