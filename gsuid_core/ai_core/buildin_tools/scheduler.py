"""
add_scheduled_task 工具模块

为主 Agent 提供预约定时任务的能力。
当用户需要为未来某个时间执行复杂任务时调用此工具。
"""

import uuid
from datetime import datetime

from pydantic_ai import RunContext

from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask


@ai_tools(category="buildin")
async def add_scheduled_task(
    ctx: RunContext[ToolContext],
    run_time: str,
    task_prompt: str,
) -> str:
    """
    预约一个未来时间执行的 AI 任务

    当你需要为用户设定未来某个时间执行的复杂任务时调用此工具。
    例如：用户说"明天早上6点帮我查一下英伟达的股价"，你就可以调用此工具。

    注意：
    - task_prompt 必须非常详细，包含需要查询的实体和需要返回的格式
    - run_time 格式必须为 "YYYY-MM-DD HH:MM:SS"
    - 任务执行时会加载当时的 persona 来执行任务，保持语气一致

    Args:
        ctx: 工具执行上下文（包含 bot 和 ev 对象）
        run_time: 执行时间，格式 "YYYY-MM-DD HH:MM:SS"
        task_prompt: 具体要执行的任务描述，请详细描述任务需求

    Returns:
        确认信息，包含执行时间和任务内容

    Example:
        >>> result = await add_scheduled_task(
        ...     ctx,
        ...     run_time="2024-05-15 06:30:00",
        ...     task_prompt="查询英伟达(NVDA)的实时股价和最新新闻，并总结成一段简洁的汇报",
        ... )
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev

    if ev is None:
        return "⚠️ 无法获取事件信息，预约失败"

    # 解析执行时间
    try:
        trigger_time = datetime.strptime(run_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return f"⚠️ 时间格式错误，请使用 YYYY-MM-DD HH:MM:SS 格式，输入的时间是: {run_time}"

    # 检查时间是否在未来
    if trigger_time <= datetime.now():
        return f"⚠️ 预约时间必须在未来，当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    # 生成唯一任务ID
    task_id = f"scheduled_task_{uuid.uuid4().hex[:12]}"

    # 获取 session_id 和 persona_name
    session_id = ev.session_id

    # 尝试获取 persona_name
    persona_name = None
    try:
        from gsuid_core.ai_core.persona import persona_config_manager

        persona_name = persona_config_manager.get_persona_for_session(session_id)
    except Exception as e:
        logger.warning(f"⚠️ [ScheduledTask] 获取 persona_name 失败: {e}")

    # 延迟导入 executor 避免循环引用
    from gsuid_core.ai_core.scheduled_task.executor import execute_scheduled_task

    try:
        # 使用 Event 中的信息创建数据库记录
        await AIScheduledTask.full_insert_data(
            task_id=task_id,
            bot_id=ev.bot_id,
            user_id=ev.user_id,
            group_id=ev.group_id,
            bot_self_id=getattr(ev, "bot_self_id", "") or "",
            user_type=ev.user_type or "direct",
            WS_BOT_ID=getattr(ev, "WS_BOT_ID", None),
            persona_name=persona_name,
            session_id=session_id,
            trigger_time=trigger_time,
            task_prompt=task_prompt,
            status="pending",
        )

        # 注册到 APScheduler
        scheduler.add_job(
            func=execute_scheduled_task,
            trigger="date",
            run_date=trigger_time,
            args=[task_id],
            id=task_id,
            replace_existing=True,
        )

        persona_info = f", persona={persona_name}" if persona_name else ""
        logger.info(
            f"⏰ [ScheduledTask] 预约任务成功: task_id={task_id}, "
            f"user_id={ev.user_id}, session_id={session_id}{persona_info}, "
            f"trigger_time={run_time}"
        )

        return f"✅ 任务预约成功！\n📅 执行时间：{run_time}\n📝 任务内容：{task_prompt}\n🔖 任务ID：{task_id}"

    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 预约任务失败: {e}")
        return f"⚠️ 预约任务失败: {str(e)}"
