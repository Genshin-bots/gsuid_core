import inspect
from typing import Any, List
from concurrent.futures import ThreadPoolExecutor

from apscheduler.job import Job
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
