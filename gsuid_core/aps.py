import inspect
from concurrent.futures import ThreadPoolExecutor

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from gsuid_core.logger import logger
from gsuid_core.config import core_config

misfire_grace_time = core_config.get_config('misfire_grace_time')

executor = ThreadPoolExecutor(max_workers=10)
job_defaults = {'misfire_grace_time': misfire_grace_time, 'coalesce': True}
options = {
    'executor': executor,
    'job_defaults': job_defaults,
    'timezone': 'Asia/Shanghai',
}
scheduler = AsyncIOScheduler()
scheduler.configure(options)


async def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        logger.info('⏲ [定时器系统] 定时任务启动成功！')


async def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info('⌛ [定时器系统] 程序关闭！定时任务结束！')


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
                logger.warning(
                    f'发现重复函数名定时任务{i.name}, 移除该任务...'
                )
                scheduler.remove_job(i.id)

    del repeat_jobs
