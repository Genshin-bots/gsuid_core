"""
execute_scheduled_task 定时执行器

当 APScheduler 触发定时任务时，调用此函数执行。

执行模型（§3.3 改造后）：
- 不再把任务 prompt 当 user_message 喂给真用户主 session（否则会污染主
  session 的 ``self.history`` / ``message_history`` / ``session_logger``）。
- 改成派一个 SubAgent 形态的"执行体"——独立 ``session_id`` + ``is_subagent=True``
  + 任务对应 persona 的 system_prompt，任务 prompt 只在 SubAgent 内出现。
- 结果通过 ``emit_proactive_message(source="scheduled_task")`` 统一播报，
  由它一并完成 bot.send / message_history / 主 session 同步 / C8 网关登记。

支持两种任务类型：
- once: 一次性任务，执行后状态变为 executed
- interval: 循环任务，执行后检查是否达到最大执行次数，若未达到则更新下次执行时间
"""

import time
from typing import Optional
from datetime import datetime, timedelta

from pytz import timezone

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event

from .models import AIScheduledTask

TZ_SHANGHAI = timezone("Asia/Shanghai")


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """将 offset-naive datetime 转换为 offset-aware datetime（上海时区）

    数据库中存储的 datetime 通常是 offset-naive 的，而代码中使用
    datetime.now(TZ_SHANGHAI) 生成的是 offset-aware 的。
    此函数确保两者可以安全比较。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_SHANGHAI)
    return dt


# 安全限制：最大循环执行次数
MAX_EXECUTION_LIMIT = 150


async def execute_scheduled_task(task_id: str) -> None:
    """
    执行定时 AI 任务

    这是被 APScheduler 调用的统一执行器。当时间到达时，
    此函数会：
    1. 从数据库读取任务信息
    2. 构建 Event 对象
    3. 派 SubAgent 执行体（独立 session、独立日志、不污染主 session）
    4. 通过 emit_proactive_message 把结果以"主动消息"形式播报给用户，同时
       同步进主 session 的 pydantic_ai 历史与 session_logger
    5. 对于循环任务，更新下次执行时间或标记为已完成

    Args:
        task_id: 任务ID，从 APScheduler 传递
    """
    from gsuid_core.aps import scheduler
    from gsuid_core.gss import gss
    from gsuid_core.ai_core.configs.ai_config import ai_config

    # 检查AI总开关
    if not ai_config.get_config("enable").data:
        logger.info(f"⏰ [ScheduledTask] AI总开关已关闭，跳过执行定时任务: {task_id}")
        return

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

    # 4. 派一个 SubAgent 形态的"执行体"完成任务，结果再通过 emitter 播报。
    #    与旧路径的区别：**任务 prompt 不再被当作 user_message 喂给真用户主
    #    session**——主用户 session 不再被伪 user_input"【定时任务执行】..."污染。
    try:
        from gsuid_core.ai_core.persona import build_persona_prompt
        from gsuid_core.ai_core.gs_agent import GsCoreAIAgent, create_agent
        from gsuid_core.ai_core.proactive import emit_proactive_message
        from gsuid_core.ai_core.statistics.manager import statistics_manager

        logger.info(
            f"🧠 [ScheduledTask] 启动定时任务 SubAgent: session_id={task.session_id}, persona={task.persona_name}"
        )

        # 记录触发方式为 scheduled
        statistics_manager.record_trigger(trigger_type="scheduled")

        # 构建任务消息（含结构化上下文与上次执行摘要）
        context_block = ""
        if task.structured_context:
            context_block += f"\n\n【结构化上下文】\n{task.structured_context}"
        if task.last_result_summary:
            context_block += f"\n\n【上次执行结果摘要】\n{task.last_result_summary}"
        if task.task_type == "interval":
            exec_no = (task.current_executions or 0) + 1
            context_block += f"\n\n【执行信息】这是第 {exec_no} 次执行"
        task_message = (
            "【定时任务执行】请完成以下任务，直接输出结果（不要有多余的解释）："
            f"\n\n任务内容：{task.task_prompt}{context_block}"
        )

        # 用任务对应 persona 构造 SubAgent；session_id 独立于真用户 session，
        # 任务 prompt 只在 SubAgent 内当 user_message 出现。
        exec_session_id = f"sched_task_{task.task_id}_{int(time.time())}"
        persona_prompt = ""
        if task.persona_name:
            persona_prompt = await build_persona_prompt(task.persona_name)

        sub_agent: GsCoreAIAgent = create_agent(
            system_prompt=persona_prompt or None,
            persona_name=task.persona_name or None,
            create_by="ScheduledTask_Exec",
            session_id=exec_session_id,
            is_subagent=True,
        )
        sub_agent_logger = sub_agent._session_logger
        sub_agent_log_files: list[str] = []

        # 通过 SubAgent 执行任务（return_mode="return"：拿到结果文本，先不发，
        # 由 emitter 统一播报；避免 SubAgent 工具池里没有 send_chat_result 也能
        # 完成"由框架代发"的效果）。run 异常时由外层 try/except 捕获并落库；
        # finally 保证 SubAgent logger 无论如何都关闭，避免轮询任务堆积。
        try:
            result: str = await sub_agent.run(
                user_message=task_message,
                bot=bot_instance,
                ev=ev,
                return_mode="return",
            )
        finally:
            if sub_agent_logger is not None:
                sub_agent_log_files.append(str(sub_agent_logger._file_path))
                sub_agent_logger.close()

        # 截取本次执行结果摘要，供下次执行参考
        result_summary: Optional[str] = str(result)[:200] if result else None

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
                        "executed_at": datetime.now(TZ_SHANGHAI),
                        "current_executions": current_exec,
                        "result": result,
                        "last_result_summary": result_summary,
                    },
                )
                logger.info(
                    f"✅ [ScheduledTask] 循环任务执行完毕（已达最大次数）: task_id={task_id}, 执行了 {current_exec} 次"
                )
            else:
                # 更新下次执行时间
                interval_sec = task.interval_seconds or 0
                next_run = datetime.now(TZ_SHANGHAI) + timedelta(seconds=interval_sec)

                # 如果任务处于 paused 状态，不要重新注册
                if task.status == "pending":
                    # 重新注册到调度器
                    if scheduler.get_job(task_id):
                        scheduler.remove_job(task_id)

                    scheduler.add_job(
                        func=execute_scheduled_task,
                        trigger="interval",
                        seconds=interval_sec,
                        start_date=datetime.now(TZ_SHANGHAI),
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
                        "last_result_summary": result_summary,
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
                    "executed_at": datetime.now(TZ_SHANGHAI),
                    "result": result,
                    "last_result_summary": result_summary,
                },
            )

        # 6. 推送结果给用户：走统一 emitter，由它一并完成
        #    bot.send / message_history (proactive metadata) / 主 session 同步
        #    （append_proactive_assistant_turn → pydantic_ai 历史 + proactive_emission
        #    entry）/ C8 网关 register_send。
        if result:
            sent = await emit_proactive_message(
                event=ev,
                message=str(result),
                source="scheduled_task",
                trigger_reason=f"task_id={task_id}",
                generator_log_files=sub_agent_log_files,
                bot=bot_instance,
                suppress_when_heartbeat_recent=False,
            )
            if sent:
                logger.info(f"✅ [ScheduledTask] 任务执行成功并已推送: task_id={task_id}")
            else:
                logger.warning(f"⚠️ [ScheduledTask] 任务结果发送被抑制 / Bot 不可用: task_id={task_id}")
        else:
            logger.warning(f"⚠️ [ScheduledTask] 任务结果为空，未推送: task_id={task_id}")

    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 任务执行失败: {task_id}, error={e}")

        # 更新任务状态为失败；同时把失败信息写入 last_result_summary，
        # 否则循环任务下次执行会读到上一次"成功"的过期摘要，无从得知上次已失败。
        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data={
                "status": "failed",
                "executed_at": datetime.now(TZ_SHANGHAI),
                "error_message": str(e),
                "last_result_summary": f"[上次执行失败] {str(e)[:150]}",
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
            next_run = _ensure_aware(task.next_run_time)
            if next_run and next_run > datetime.now(TZ_SHANGHAI):
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
            trigger = _ensure_aware(task.trigger_time)
            if trigger and trigger <= datetime.now(TZ_SHANGHAI):
                # 立即执行
                logger.info(f"⏰ [ScheduledTask] 发现已到期的一次性任务，立即执行: {task.task_id}")
                await execute_scheduled_task(task.task_id)
            elif trigger:
                # 重新注册到调度器
                scheduler.add_job(
                    func=execute_scheduled_task,
                    trigger="date",
                    run_date=trigger,
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
