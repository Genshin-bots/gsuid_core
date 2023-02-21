from apscheduler.schedulers.asyncio import AsyncIOScheduler

from gsuid_core.logger import logger

scheduler = AsyncIOScheduler()


async def start_scheduler():
    if not scheduler.running:
        scheduler.start()
        logger.info('定时任务启动...')


async def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info('定时任务结束...')
