import inspect
from typing import Any, List, Literal, Callable, Optional, Annotated
from concurrent.futures import ThreadPoolExecutor

from msgspec import Meta
from apscheduler.job import Job
from ai_core.register import ai_tools
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from gsuid_core.config import core_config
from gsuid_core.logger import logger

misfire_grace_time = core_config.get_config("misfire_grace_time")

executor = ThreadPoolExecutor(max_workers=10)
job_defaults = {"misfire_grace_time": misfire_grace_time, "coalesce": True}
options = {
    "executor": executor,
    "job_defaults": job_defaults,
    "timezone": "Asia/Shanghai",
}
scheduler = AsyncIOScheduler()
scheduler.configure(options)


async def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        logger.info("⏲ [定时器系统] 定时任务启动成功！")


async def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("⌛ [定时器系统] 程序关闭！定时任务结束！")


def remove_repeat_job():
    repeat_jobs = {}
    for i in scheduler.get_jobs():
        if i.name not in repeat_jobs:
            repeat_jobs[i.name] = i
        else:
            source_i = inspect.getsource(repeat_jobs[i.name].func)
            source_j = inspect.getsource(i.func)
            if source_i == source_j:
                scheduler.remove_job(i.id)
            else:
                logger.warning(f"发现重复函数名定时任务{i.name}, 移除该任务...")
                scheduler.remove_job(i.id)

    del repeat_jobs


def get_all_aps_job() -> List[Job]:
    return scheduler.get_jobs()


def _get_trigger_description(trigger: Any) -> str:
    """根据触发器类型，生成易读的运行规律描述"""
    from datetime import datetime

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    if isinstance(trigger, DateTrigger):
        if isinstance(trigger.run_date, datetime):
            return f"一次性运行：{trigger.run_date.strftime('%Y-%m-%d %H:%M:%S')}"
        return "一次性运行：未知时间"

    elif isinstance(trigger, IntervalTrigger):
        # interval_str = str(trigger.interval)
        # Convert interval to a readable format (e.g., "每 1 时 30 分")
        hours, remainder = divmod(trigger.interval.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts = []
        if hours:
            parts.append(f"{hours} 时")
        if minutes:
            parts.append(f"{minutes} 分")
        if seconds:
            parts.append(f"{seconds} 秒")
        return f"间隔运行：每 {' '.join(parts)}运行一次"

    elif isinstance(trigger, CronTrigger):
        fields_info = {}
        for field in trigger.fields:
            expression = str(field)
            if expression != "*" and expression is not None:
                fields_info[field.name] = expression

        date_parts = []
        time_parts = []

        if "day_of_week" in fields_info:
            dow = fields_info["day_of_week"]
            dow_map = {
                "mon": "周一",
                "tue": "周二",
                "wed": "周三",
                "thu": "周四",
                "fri": "周五",
                "sat": "周六",
                "sun": "周日",
            }
            dow_display = dow_map.get(dow.lower(), dow)
            date_parts.append(f"每周的 {dow_display}")
        if "month" in fields_info and "day" in fields_info:
            date_parts.append(f"每 {fields_info['month']} 月的 {fields_info['day']} 日")
        elif "day" in fields_info:
            date_parts.append(f"每月的 {fields_info['day']} 日")

        if "hour" in fields_info:
            time_parts.append(f"{fields_info['hour']} 时")
        if "minute" in fields_info:
            time_parts.append(f"{fields_info['minute']} 分")
        if "second" in fields_info and fields_info["second"] != "0":
            time_parts.append(f"{fields_info['second']} 秒")

        date_desc = "".join(date_parts).strip()
        time_desc = "".join(time_parts).strip()

        if date_desc and time_desc:
            final_desc = f"{date_desc} {time_desc}"
        elif date_desc:
            final_desc = date_desc
        elif time_desc:
            final_desc = f"每天 {time_desc}"
        else:
            # Check if any fields are non-default to avoid incorrect "每分钟"
            if fields_info:
                final_desc = "自定义调度（复杂表达式）"
            else:
                final_desc = "每分钟"

        return f"Cron 运行：{final_desc}"

    return "未知触发器类型"


@ai_tools
def add_scheduled_job(
    func: Annotated[
        Callable,
        Meta(description="要执行的异步或同步函数"),
    ],
    trigger_type: Annotated[
        Literal["date", "interval", "cron"],
        Meta(description="触发器类型: date(一次性任务), interval(间隔任务), cron(Cron表达式任务)，默认为 cron"),
    ] = "cron",
    job_id: Annotated[
        Optional[str],
        Meta(description="定时任务的唯一标识ID，如果不提供则自动生成"),
    ] = None,
    job_name: Annotated[
        Optional[str],
        Meta(description="定时任务的名称，用于标识任务"),
    ] = None,
    replace_existing: Annotated[
        bool,
        Meta(description="如果已存在相同ID的任务是否替换，默认为 True"),
    ] = True,
    # DateTrigger 参数
    run_date: Annotated[
        Optional[str],
        Meta(description="[DateTrigger] 任务执行的具体日期时间，ISO格式字符串如 2024-12-31T23:59:59"),
    ] = None,
    # IntervalTrigger 参数
    weeks: Annotated[
        int,
        Meta(description="[IntervalTrigger] 间隔的周数"),
    ] = 0,
    days: Annotated[
        int,
        Meta(description="[IntervalTrigger] 间隔的天数"),
    ] = 0,
    hours: Annotated[
        int,
        Meta(description="[IntervalTrigger] 间隔的小时数"),
    ] = 0,
    minutes: Annotated[
        int,
        Meta(description="[IntervalTrigger] 间隔的分钟数"),
    ] = 0,
    seconds: Annotated[
        int,
        Meta(description="[IntervalTrigger] 间隔的秒数"),
    ] = 0,
    start_date: Annotated[
        Optional[str],
        Meta(description="[IntervalTrigger] 间隔任务的开始日期时间，ISO格式字符串"),
    ] = None,
    end_date: Annotated[
        Optional[str],
        Meta(description="[IntervalTrigger] 间隔任务的结束日期时间，ISO格式字符串"),
    ] = None,
    # CronTrigger 参数
    year: Annotated[
        Optional[str],
        Meta(description="[CronTrigger] 4位数年份，如 2024 或 2020-2025"),
    ] = None,
    month: Annotated[
        Optional[str],
        Meta(description="[CronTrigger] 月份，1-12，支持范围如 1-6 或逗号分隔 1,3,5"),
    ] = None,
    day: Annotated[
        Optional[str],
        Meta(description="[CronTrigger] 日期，1-31，支持范围如 1-15 或逗号分隔 1,15"),
    ] = None,
    hour: Annotated[
        Optional[str],
        Meta(description="[CronTrigger] 小时，0-23，支持范围如 0-6 或逗号分隔 0,12,18"),
    ] = None,
    minute: Annotated[
        Optional[str],
        Meta(description="[CronTrigger] 分钟，0-59，支持范围如 0-30 或逗号分隔 0,30"),
    ] = None,
    second: Annotated[
        Optional[str],
        Meta(description="[CronTrigger] 秒数，0-59，支持范围如 0-30 或逗号分隔 0,30"),
    ] = None,
    day_of_week: Annotated[
        Optional[str],
        Meta(description="[CronTrigger] 星期几，0-6 或 mon,tue,wed,thu,fri,sat,sun，支持逗号分隔 mon,wed,fri"),
    ] = None,
) -> Optional[Job]:
    """添加定时任务的通用函数

    支持三种触发器类型：date（一次性任务）、interval（间隔任务）、cron（Cron表达式任务）

    Args:
        func: 要执行的异步或同步函数
        trigger_type: 触发器类型，可选值为 "date"、"interval"、"cron"，默认为 "cron"
        job_id: 定时任务的唯一标识ID，如果不提供则自动生成
        job_name: 定时任务的名称，用于标识任务
        replace_existing: 如果已存在相同ID的任务是否替换，默认为 True

        # DateTrigger 参数 (trigger_type="date" 时使用)
        run_date: 任务执行的具体日期时间，可以是 datetime 对象或 ISO 格式的字符串

        # IntervalTrigger 参数 (trigger_type="interval" 时使用)
        weeks: 间隔的周数
        days: 间隔的天数
        hours: 间隔的小时数
        minutes: 间隔的分钟数
        seconds: 间隔的秒数
        start_date: 间隔任务的开始日期时间，默认为当前时间
        end_date: 间隔任务的结束日期时间，可选

        # CronTrigger 参数 (trigger_type="cron" 时使用)
        year: 4位数年份，如 2024 或 "2024"（支持范围如 "2020-2025"）
        month: 月份，1-12（支持范围如 "1-6" 或逗号分隔 "1,3,5"）
        day: 日期，1-31（支持范围如 "1-15" 或逗号分隔 "1,15"）
        hour: 小时，0-23（支持范围如 "0-6" 或逗号分隔 "0,12,18"）
        minute: 分钟，0-59（支持范围如 "0-30" 或逗号分隔 "0,30"）
        second: 秒数，0-59（支持范围如 "0-30" 或逗号分隔 "0,30"）
        day_of_week: 星期几，0-6 或 mon,tue,wed,thu,fri,sat,sun（支持逗号分隔 "mon,wed,fri"）

    Returns:
        添加的 Job 对象，如果添加失败则返回 None

    Examples:
        # 1. 每分钟执行一次
        add_scheduled_job(my_func, trigger_type="cron", minute="*")

        # 2. 每天早上8点执行
        add_scheduled_job(my_func, trigger_type="cron", hour=8, minute=0)

        # 3. 每周一、周三、周五的早上9点执行
        add_scheduled_job(my_func, trigger_type="cron", day_of_week="mon,wed,fri", hour=9, minute=0)

        # 4. 每天每隔1小时执行一次
        add_scheduled_job(my_func, trigger_type="interval", hours=1)

        # 5. 每30分钟执行一次
        add_scheduled_job(my_func, trigger_type="interval", minutes=30)

        # 6. 在指定时间执行一次（一次性任务）
        from datetime import datetime
        add_scheduled_job(my_func, trigger_type="date", run_date=datetime(2024, 12, 31, 23, 59, 59))

        # 7. 带自定义job_id和job_name
        add_scheduled_job(
            my_func,
            trigger_type="cron",
            job_id="my_custom_job",
            job_name="我的自定义任务",
            hour=10, minute=30, second=0, day_of_week="mon,wed,fri",
        )
    """
    from datetime import datetime

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    # 验证 trigger_type
    valid_trigger_types = ["date", "interval", "cron"]
    if trigger_type not in valid_trigger_types:
        logger.error(f"[定时器系统] 无效的触发器类型: {trigger_type}，仅支持 {valid_trigger_types}")
        return None

    # 生成 job_id 如果没有提供
    if job_id is None:
        import uuid

        job_id = f"job_{uuid.uuid4().hex[:8]}"

    # 根据 trigger_type 创建对应的触发器
    trigger = None
    try:
        if trigger_type == "date":
            if run_date is None:
                logger.error("[定时器系统] DateTrigger 必须提供 run_date 参数")
                return None
            # 如果是字符串，尝试转换为 datetime
            run_date_dt = datetime.fromisoformat(run_date) if isinstance(run_date, str) else run_date
            trigger = DateTrigger(run_date=run_date_dt)

        elif trigger_type == "interval":
            # 转换字符串日期时间
            start_date_dt = None
            end_date_dt = None
            if start_date is not None:
                start_date_dt = datetime.fromisoformat(start_date)
            if end_date is not None:
                end_date_dt = datetime.fromisoformat(end_date)

            trigger = IntervalTrigger(
                weeks=weeks,
                days=days,
                hours=hours,
                minutes=minutes,
                seconds=seconds,
                start_date=start_date_dt,
                end_date=end_date_dt,
            )

        elif trigger_type == "cron":
            # CronTrigger 参数
            cron_fields = {}
            if year is not None:
                cron_fields["year"] = year
            if month is not None:
                cron_fields["month"] = month
            if day is not None:
                cron_fields["day"] = day
            if hour is not None:
                cron_fields["hour"] = hour
            if minute is not None:
                cron_fields["minute"] = minute
            if second is not None:
                cron_fields["second"] = second
            if day_of_week is not None:
                cron_fields["day_of_week"] = day_of_week

            trigger = CronTrigger(**cron_fields)

    except Exception as e:
        logger.error(f"[定时器系统] 创建触发器失败: {e}")
        return None

    # 添加任务到调度器
    try:
        job = scheduler.add_job(
            func=func,
            trigger=trigger,
            id=job_id,
            name=job_name or job_id,
            replace_existing=replace_existing,
        )
        logger.info(
            f"[定时器系统] 成功添加定时任务: {job_id} ({job_name or '未命名'}) - {_get_trigger_description(trigger)}"
        )
        return job

    except Exception as e:
        logger.error(f"[定时器系统] 添加定时任务失败: {e}")
        return None
