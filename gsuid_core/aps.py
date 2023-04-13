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
        logger.info('定时任务启动...')


async def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info('定时任务结束...')
