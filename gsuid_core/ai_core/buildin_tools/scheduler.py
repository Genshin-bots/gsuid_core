"""
定时/循环任务 AI 工具模块

为主 Agent 提供预约定时/循环任务的能力，支持增删改查启停。
每个 action 类型对应一个独立的 AI 工具函数。

## 任务类型

1. **一次性任务 (once)**: 在指定时间点执行一次，适用于"明天叫我起床"、"周五提醒我交报告"等场景。
2. **循环任务 (interval)**: 按固定间隔重复执行，适用于"每半小时查一下股价"、"每天早上发天气预报"等场景。

## 安全限制

- 单用户最多 20 个待执行任务
- 循环任务最大执行次数为 10 次
- 循环任务最小间隔为 5 分钟

## 状态说明

- pending: 待执行
- paused: 已暂停（仅循环任务支持）
- executed: 已执行完毕
- failed: 执行失败
- cancelled: 已取消
"""

import uuid
from typing import Optional
from datetime import datetime, timedelta

from pytz import timezone
from pydantic_ai import RunContext

from gsuid_core.aps import scheduler
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.scheduled_task.models import AIScheduledTask

TZ_SHANGHAI = timezone("Asia/Shanghai")

# 安全限制
MAX_PENDING_TASKS_PER_USER = 20
MAX_EXECUTION_LIMIT = 10
MIN_INTERVAL_SECONDS = 300


def _get_session_info(ev) -> tuple[Optional[str], Optional[str]]:
    """获取 session_id 和 persona_name"""
    session_id = ev.session_id
    persona_name = None
    try:
        from gsuid_core.ai_core.persona import persona_config_manager

        persona_name = persona_config_manager.get_persona_for_session(session_id)
    except Exception as e:
        logger.warning(f"⚠️ [ScheduledTask] 获取 persona_name 失败: {e}")
    return session_id, persona_name


def _get_execute_scheduled_task():
    """延迟导入 executor"""
    from gsuid_core.ai_core.scheduled_task.executor import execute_scheduled_task

    return execute_scheduled_task


# ============ 添加任务 ============


@ai_tools(category="self")
async def add_once_task(
    ctx: RunContext[ToolContext],
    run_time: str,
    task_prompt: str,
) -> str:
    """
    添加一次性定时任务

    在指定时间点执行一次任务。适用于用户说"明天早上6点叫我起床"、"周六晚上8点提醒我开会"等场景。

    当用户需要为未来某个具体时间点安排一个任务时调用此工具。任务执行时会加载当前的 persona，
    保持与主 Agent 一致的语气和风格。

    Args:
        ctx: 工具执行上下文
        run_time: 执行时间，格式 "YYYY-MM-DD HH:MM:SS"
        task_prompt: 任务描述，应该清晰说明需要做什么以及期望的输出格式

    Returns:
        操作结果信息，包含任务ID供后续查询/取消使用

    Examples:
        # 用户说"明天早上6点叫我起床"
        >>> await add_once_task(
        ...     ctx,
        ...     run_time="2024-05-15 06:00:00",
        ...     task_prompt="用温柔的语气叫用户起床，说'早上好呀，该起床了哦~'",
        ... )

        # 用户说"周五晚上8点提醒我开会"
        >>> await add_once_task(
        ...     ctx,
        ...     run_time="2024-05-17 20:00:00",
        ...     task_prompt="提醒用户开会，说'主人，晚上8点有会议哦，记得参加~'",
        ... )

        # 用户说"1小时后提醒我喝水"
        >>> await add_once_task(
        ...     ctx,
        ...     run_time="2024-05-14 16:30:00",
        ...     task_prompt="温柔地提醒用户喝水，说'主人，记得多喝水对身体好哦~'",
        ... )
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None:
        return "⚠️ 无法获取事件信息，操作失败"

    session_id, persona_name = _get_session_info(ev)
    execute_scheduled_task = _get_execute_scheduled_task()

    # 验证参数
    if not task_prompt:
        return "⚠️ 添加任务失败：缺少 task_prompt（任务描述）"

    if not run_time:
        return "⚠️ 添加任务失败：缺少 run_time"

    # 解析时间（统一使用 Asia/Shanghai 时区）
    try:
        trigger_time = datetime.strptime(run_time, "%Y-%m-%d %H:%M:%S")
        trigger_time = TZ_SHANGHAI.localize(trigger_time)
    except ValueError:
        return "⚠️ 时间格式错误，请使用 YYYY-MM-DD HH:MM:SS 格式"

    now_shanghai = datetime.now(TZ_SHANGHAI)
    if trigger_time <= now_shanghai:
        return f"⚠️ 预约时间必须在未来，当前时间: {now_shanghai.strftime('%Y-%m-%d %H:%M:%S')}"

    # 安全检查
    existing_tasks = await AIScheduledTask.select_rows(user_id=ev.user_id)
    user_pending_count = sum(1 for t in existing_tasks if t.status == "pending")
    if user_pending_count >= MAX_PENDING_TASKS_PER_USER:
        return f"⚠️ 您已有 {MAX_PENDING_TASKS_PER_USER} 个待执行任务，请先取消一些任务"

    # 生成任务ID
    task_id = f"scheduled_task_{uuid.uuid4().hex[:12]}"

    try:
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
            task_type="once",
            trigger_time=trigger_time,
            task_prompt=task_prompt,
            status="pending",
            next_run_time=trigger_time,
        )

        scheduler.add_job(
            func=execute_scheduled_task,
            trigger="date",
            run_date=trigger_time,
            args=[task_id],
            id=task_id,
            replace_existing=True,
        )

        return f"✅ 一次性任务添加成功！\n📋 任务ID：{task_id}\n📅 执行时间：{run_time}\n📝 任务内容：{task_prompt}"

    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 添加任务失败: {e}")
        return f"⚠️ 添加任务失败: {str(e)}"


@ai_tools(category="self")
async def add_interval_task(
    ctx: RunContext[ToolContext],
    interval_value: int,
    task_prompt: str,
    start_time: str,
    interval_type: str = "minutes",
    max_executions: int = 10,
) -> str:
    """
    添加循环任务

    按固定间隔重复执行任务。当用户需要定期执行某个任务时调用此工具，
    例如"每半小时查一下股价"、"每天早上发天气预报"、"每天下午3点30分查xxx"。

    循环任务会按照设定的时间间隔重复执行，达到最大执行次数后自动结束。
    系统安全限制：最大执行10次，最小间隔5分钟。

    Args:
        ctx: 工具执行上下文
        interval_value: 间隔值，配合 interval_type 使用
        task_prompt: 任务描述，应该清晰说明需要做什么
        interval_type: 间隔类型，"minutes"(分钟)/"hours"(小时)/"days"(天)，默认 "minutes"
        max_executions: 最大执行次数，默认 10 次（安全限制，不可超过）
        start_time: 首次执行的时间，格式 "YYYY-MM-DD HH:MM:SS"
                    例如用户说"每天下午3点30分"，则 start_time="2024-05-15 15:30:00"

    Returns:
        操作结果信息，包含任务ID供后续查询/暂停/取消使用

    Examples:
        # 用户说"每半小时帮我查一下英伟达的股价"
        >>> await add_interval_task(
        ...     ctx,
        ...     interval_value=30,
        ...     interval_type="minutes",
        ...     task_prompt="查询英伟达(NVDA)的当前股价，如果涨跌幅超过2%则提醒用户。",
        ...     max_executions=10,
        ... )

        # 用户说"每天早上8点给我发天气预报"
        >>> await add_interval_task(
        ...     ctx,
        ...     interval_value=1,
        ...     task_prompt="查询今天的天气预报，以简洁友好的语气回复，格式如'今天天气XX度，XX天气，记得带伞哦~'",
        ...     interval_type="days",
        ...     max_executions=10,
        ...     start_time="2024-05-15 08:00:00",
        ... )

        # 用户说"每天下午3点30分查xxx"
        >>> await add_interval_task(
        ...     ctx,
        ...     interval_value=1,
        ...     task_prompt="查询xxx的最新信息",
        ...     interval_type="days",
        ...     max_executions=10,
        ...     start_time="2024-05-15 15:30:00",
        ... )

        # 用户说"每2小时提醒我站起来活动一下"
        >>> await add_interval_task(
        ...     ctx,
        ...     interval_value=2,
        ...     interval_type="hours",
        ...     task_prompt="用关心的语气提醒用户站起来活动一下，说'主人，久坐对身体不好哦，起来动一动吧~'",
        ...     max_executions=10,
        ... )
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None:
        return "⚠️ 无法获取事件信息，操作失败"

    session_id, persona_name = _get_session_info(ev)
    execute_scheduled_task = _get_execute_scheduled_task()

    # 验证参数
    if not task_prompt:
        return "⚠️ 添加任务失败：缺少 task_prompt（任务描述）"

    if interval_value <= 0:
        return "⚠️ 间隔值必须大于 0"

    # 解析 start_time (YYYY-MM-DD HH:MM:SS 格式)
    start_datetime_value: Optional[datetime] = None

    if start_time:
        try:
            start_datetime_value = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
            start_datetime_value = TZ_SHANGHAI.localize(start_datetime_value)
        except ValueError:
            return "⚠️ start_time 格式错误，请使用 'YYYY-MM-DD HH:MM:SS' 格式"

    # 转换为秒
    if interval_type == "minutes":
        interval_seconds = interval_value * 60
    elif interval_type == "hours":
        interval_seconds = interval_value * 3600
    elif interval_type == "days":
        interval_seconds = interval_value * 86400
    else:
        return f"⚠️ 未知间隔类型: {interval_type}"

    # 安全检查
    if interval_seconds < MIN_INTERVAL_SECONDS:
        return f"⚠️ 循环任务最小间隔为 {MIN_INTERVAL_SECONDS // 60} 分钟"

    if max_executions > MAX_EXECUTION_LIMIT:
        max_executions = MAX_EXECUTION_LIMIT
    if max_executions <= 0:
        return "⚠️ 最大执行次数必须大于 0"

    # 安全检查：用户任务数
    existing_tasks = await AIScheduledTask.select_rows(user_id=ev.user_id)
    user_pending_count = sum(1 for t in existing_tasks if t.status == "pending")
    if user_pending_count >= MAX_PENDING_TASKS_PER_USER:
        return f"⚠️ 您已有 {MAX_PENDING_TASKS_PER_USER} 个待执行任务，请先取消一些任务"

    # 生成任务ID
    task_id = f"scheduled_task_{uuid.uuid4().hex[:12]}"

    try:
        now_shanghai = datetime.now(TZ_SHANGHAI)

        # 计算下次执行时间
        if start_datetime_value is not None:
            # 用户指定了开始时间 (YYYY-MM-DD HH:MM:SS 格式)
            # 从指定时间开始按间隔执行
            if start_datetime_value <= now_shanghai:
                return f"⚠️ 开始时间必须在未来，当前时间: {now_shanghai.strftime('%Y-%m-%d %H:%M:%S')}"
            next_run = start_datetime_value
            trigger = None
            start_date_for_interval = start_datetime_value
            start_time_display = f"从 {start_datetime_value.strftime('%Y-%m-%d %H:%M')} 开始"
        else:
            # 使用 interval trigger，从当前时间开始
            next_run = now_shanghai + timedelta(seconds=interval_seconds)
            trigger = None
            start_date_for_interval = now_shanghai
            start_time_display = None

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
            task_type="interval",
            task_prompt=task_prompt,
            status="pending",
            interval_seconds=interval_seconds,
            max_executions=max_executions,
            current_executions=0,
            start_time=now_shanghai,
            next_run_time=next_run,
        )

        # 添加调度任务
        if trigger:
            # 使用 cron trigger（每天固定时间执行）
            scheduler.add_job(
                func=execute_scheduled_task,
                trigger=trigger,
                args=[task_id],
                id=task_id,
                replace_existing=True,
            )
        else:
            # 使用 interval trigger（固定间隔执行）
            scheduler.add_job(
                func=execute_scheduled_task,
                trigger="interval",
                seconds=interval_seconds,
                start_date=start_date_for_interval,
                args=[task_id],
                id=task_id,
                replace_existing=True,
            )

        if start_time_display:
            return (
                f"✅ 循环任务添加成功！\n"
                f"📋 任务ID：{task_id}\n"
                f"⏰ 执行时间：{start_time_display}\n"
                f"🔒 最大执行次数：{max_executions}\n"
                f"📝 任务内容：{task_prompt}"
            )
        else:
            interval_unit = {"minutes": "分钟", "hours": "小时", "days": "天"}.get(interval_type, interval_type)
            return (
                f"✅ 循环任务添加成功！\n"
                f"📋 任务ID：{task_id}\n"
                f"⏰ 执行间隔：每 {interval_value} {interval_unit}\n"
                f"🔒 最大执行次数：{max_executions}\n"
                f"📝 任务内容：{task_prompt}"
            )

    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 添加任务失败: {e}")
        return f"⚠️ 添加任务失败: {str(e)}"


# ============ 查询任务 ============


@ai_tools(category="common")
async def list_scheduled_tasks(
    ctx: RunContext[ToolContext],
) -> str:
    """
    列出我创建的所有定时任务

    当用户想查看、列出自己设置过的定时任务、提醒、循环任务时调用此工具。
    触发场景如"我有哪些定时任务""看看我的提醒""我设了什么任务""任务列表"。

    Returns:
        当前用户的全部任务列表，含每个任务的 ID、类型、状态、下次执行时间
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None:
        return "⚠️ 无法获取事件信息"

    try:
        tasks = await AIScheduledTask.select_rows(user_id=ev.user_id)

        if not tasks:
            return "📋 您还没有创建任何定时任务"

        lines = ["📋 您的定时任务列表：\n", "=" * 50]

        for task_data in tasks:
            task = task_data if isinstance(task_data, AIScheduledTask) else AIScheduledTask(**task_data)

            status_emoji = {
                "pending": "⏳",
                "paused": "⏸️",
                "executed": "✅",
                "failed": "❌",
                "cancelled": "🚫",
            }.get(task.status, "❓")

            lines.append(f"\n{status_emoji} 任务ID: {task.task_id}")
            lines.append(f"   类型: {'🔄 循环' if task.task_type == 'interval' else '⏰ 一次性'}")
            lines.append(f"   状态: {task.status}")

            if task.task_type == "interval":
                interval_minutes = (task.interval_seconds or 0) // 60
                if interval_minutes >= 60:
                    interval_hours = interval_minutes // 60
                    interval_minutes = interval_minutes % 60
                    if interval_minutes > 0:
                        interval_str = f"{interval_hours}小时{interval_minutes}分钟"
                    else:
                        interval_str = f"{interval_hours}小时"
                else:
                    interval_str = f"{interval_minutes}分钟"
                lines.append(f"   间隔: 每 {interval_str}")
                lines.append(f"   执行: {task.current_executions or 0}/{task.max_executions or MAX_EXECUTION_LIMIT} 次")
            else:
                if task.trigger_time:
                    lines.append(f"   执行时间: {task.trigger_time.strftime('%Y-%m-%d %H:%M:%S')}")

            prompt = task.task_prompt or ""
            if len(prompt) > 30:
                prompt = prompt[:30] + "..."
            lines.append(f"   内容: {prompt}")

        lines.append("\n" + "=" * 50)
        return "\n".join(lines)

    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 查询任务列表失败: {e}")
        return f"⚠️ 查询任务列表失败: {str(e)}"


@ai_tools(category="common")
async def query_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,
) -> str:
    """
    查看某个定时任务的详细信息

    当用户想了解某个具体任务的完整情况时调用此工具，触发场景如
    "这个任务什么时候执行""任务 xxx 的详情""那个提醒还在吗"。

    Args:
        ctx: 工具执行上下文
        task_id: 任务 ID，从 list_scheduled_tasks 的结果或创建任务时的返回值中获取

    Returns:
        该任务的创建时间、执行时间、状态、已执行次数、上次结果等详细信息
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None:
        return "⚠️ 无法获取事件信息"

    tasks = await AIScheduledTask.select_rows(task_id=task_id)
    if not tasks:
        return f"⚠️ 任务 {task_id} 不存在"

    task = tasks[0]
    if not isinstance(task, AIScheduledTask):
        task = AIScheduledTask(**task)

    if task.user_id != ev.user_id:
        return "⚠️ 无权操作此任务"

    status_emoji = {
        "pending": "⏳",
        "paused": "⏸️",
        "executed": "✅",
        "failed": "❌",
        "cancelled": "🚫",
    }.get(task.status, "❓")

    lines = [
        f"{status_emoji} 任务详情",
        "=" * 50,
        f"📋 任务ID: {task.task_id}",
        f"🔖 类型: {'🔄 循环任务' if task.task_type == 'interval' else '⏰ 一次性任务'}",
        f"📊 状态: {task.status}",
        f"📝 任务内容: {task.task_prompt}",
    ]

    if task.task_type == "interval":
        interval_minutes = (task.interval_seconds or 0) // 60
        lines.append(f"⏰ 执行间隔: {interval_minutes} 分钟")
        lines.append(f"🔒 最大执行次数: {task.max_executions or MAX_EXECUTION_LIMIT}")
        lines.append(f"📈 已执行次数: {task.current_executions or 0}")
        if task.start_time:
            lines.append(f"🕐 开始时间: {task.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        if task.trigger_time:
            lines.append(f"🕐 执行时间: {task.trigger_time.strftime('%Y-%m-%d %H:%M:%S')}")

    if task.next_run_time:
        lines.append(f"▶️ 下次执行: {task.next_run_time.strftime('%Y-%m-%d %H:%M:%S')}")

    if task.executed_at:
        lines.append(f"✅ 最后执行: {task.executed_at.strftime('%Y-%m-%d %H:%M:%S')}")

    if task.result:
        lines.append(
            f"📌 上次结果: {task.result[:100]}..." if len(task.result) > 100 else f"📌 上次结果: {task.result}"
        )

    if task.error_message:
        lines.append(f"⚠️ 错误信息: {task.error_message}")

    lines.append(f"🕐 创建时间: {task.created_at.strftime('%Y-%m-%d %H:%M:%S')}")

    return "\n".join(lines)


# ============ 修改任务 ============


@ai_tools(category="common")
async def modify_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,
    task_prompt: Optional[str] = None,
    max_executions: Optional[int] = None,
) -> str:
    """
    修改一个定时任务的描述或最大执行次数

    当用户想调整已设置的定时任务时调用此工具，触发场景如
    "把那个任务改成…""定时任务的内容换一下""把循环次数改成 5 次"。
    只能修改 pending 或 paused 状态的任务。

    Args:
        ctx: 工具执行上下文
        task_id: 任务 ID
        task_prompt: 新的任务描述，不修改则不传
        max_executions: 新的最大执行次数，仅循环任务有效，不修改则不传

    Returns:
        操作结果信息
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None:
        return "⚠️ 无法获取事件信息"

    tasks = await AIScheduledTask.select_rows(task_id=task_id)
    if not tasks:
        return f"⚠️ 任务 {task_id} 不存在"

    task = tasks[0]
    if not isinstance(task, AIScheduledTask):
        task = AIScheduledTask(**task)

    if task.user_id != ev.user_id:
        return "⚠️ 无权操作此任务"

    if task.status not in ("pending", "paused"):
        return f"⚠️ 任务状态为 {task.status}，无法修改"

    update_data = {}

    if task_prompt is not None:
        update_data["task_prompt"] = task_prompt

    if max_executions is not None:
        if max_executions > MAX_EXECUTION_LIMIT:
            max_executions = MAX_EXECUTION_LIMIT
        if max_executions <= 0:
            return "⚠️ 最大执行次数必须大于 0"
        update_data["max_executions"] = max_executions

    if not update_data:
        return "⚠️ 未提供任何需要修改的内容"

    try:
        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data=update_data,
        )

        return f"✅ 任务已修改！\n📋 任务ID：{task_id}\n📝 更新内容：{update_data}"

    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 修改任务失败: {e}")
        return f"⚠️ 修改任务失败: {str(e)}"


# ============ 删除/取消任务 ============


@ai_tools(category="common")
async def cancel_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,
) -> str:
    """
    取消（删除）一个定时任务

    当用户想停掉、删除、不再需要某个定时任务或提醒时调用此工具，触发场景如
    "取消那个任务""别再提醒我了""删掉定时任务 xxx"。取消后任务不再执行。

    Args:
        ctx: 工具执行上下文
        task_id: 任务 ID

    Returns:
        操作结果信息
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None:
        return "⚠️ 无法获取事件信息"

    tasks = await AIScheduledTask.select_rows(task_id=task_id)
    if not tasks:
        return f"⚠️ 任务 {task_id} 不存在"

    task = tasks[0]
    if not isinstance(task, AIScheduledTask):
        task = AIScheduledTask(**task)

    if task.user_id != ev.user_id:
        return "⚠️ 无权操作此任务"

    if task.status != "pending":
        return f"⚠️ 任务状态为 {task.status}，无法取消"

    try:
        if scheduler.get_job(task_id):
            scheduler.remove_job(task_id)

        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data={"status": "cancelled"},
        )

        return f"✅ 任务已取消！\n📋 任务ID：{task_id}"

    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 取消任务失败: {e}")
        return f"⚠️ 取消任务失败: {str(e)}"


# ============ 暂停/恢复任务 ============


@ai_tools(category="common")
async def pause_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,
) -> str:
    """
    暂停一个循环任务（之后可恢复）

    当用户想暂时停止、但不彻底删除某个循环任务时调用此工具，触发场景如
    "先暂停那个任务""这阵子别执行了""暂停循环提醒"。
    仅循环任务支持暂停，一次性任务不支持。

    Args:
        ctx: 工具执行上下文
        task_id: 任务 ID

    Returns:
        操作结果信息
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None:
        return "⚠️ 无法获取事件信息"

    tasks = await AIScheduledTask.select_rows(task_id=task_id)
    if not tasks:
        return f"⚠️ 任务 {task_id} 不存在"

    task = tasks[0]
    if not isinstance(task, AIScheduledTask):
        task = AIScheduledTask(**task)

    if task.user_id != ev.user_id:
        return "⚠️ 无权操作此任务"

    if task.status != "pending":
        return f"⚠️ 任务状态为 {task.status}，无法暂停"

    if task.task_type != "interval":
        return "⚠️ 只有循环任务可以暂停"

    try:
        if scheduler.get_job(task_id):
            scheduler.pause_job(task_id)

        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data={"status": "paused"},
        )

        return f"✅ 任务已暂停！\n📋 任务ID：{task_id}"

    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 暂停任务失败: {e}")
        return f"⚠️ 暂停任务失败: {str(e)}"


@ai_tools(category="common")
async def resume_scheduled_task(
    ctx: RunContext[ToolContext],
    task_id: str,
) -> str:
    """
    恢复一个已暂停的循环任务

    当用户想让之前暂停的循环任务继续执行时调用此工具，触发场景如
    "恢复那个任务""继续之前的循环提醒""把暂停的任务开起来"。

    Args:
        ctx: 工具执行上下文
        task_id: 任务 ID

    Returns:
        操作结果信息
    """
    tool_ctx: ToolContext = ctx.deps
    ev = tool_ctx.ev
    if ev is None:
        return "⚠️ 无法获取事件信息"

    tasks = await AIScheduledTask.select_rows(task_id=task_id)
    if not tasks:
        return f"⚠️ 任务 {task_id} 不存在"

    task = tasks[0]
    if not isinstance(task, AIScheduledTask):
        task = AIScheduledTask(**task)

    if task.user_id != ev.user_id:
        return "⚠️ 无权操作此任务"

    if task.status != "paused":
        return f"⚠️ 任务状态为 {task.status}，无法恢复"

    try:
        execute_scheduled_task = _get_execute_scheduled_task()

        if scheduler.get_job(task_id):
            scheduler.remove_job(task_id)

        scheduler.add_job(
            func=execute_scheduled_task,
            trigger="interval",
            seconds=task.interval_seconds,
            start_date=datetime.now(TZ_SHANGHAI),
            args=[task_id],
            id=task_id,
            replace_existing=True,
        )

        next_run = datetime.now(TZ_SHANGHAI) + timedelta(seconds=task.interval_seconds or 0)

        await AIScheduledTask.update_data_by_data(
            select_data={"task_id": task_id},
            update_data={
                "status": "pending",
                "next_run_time": next_run,
            },
        )

        return f"✅ 任务已恢复！\n📋 任务ID：{task_id}"

    except Exception as e:
        logger.error(f"❌ [ScheduledTask] 恢复任务失败: {e}")
        return f"⚠️ 恢复任务失败: {str(e)}"
