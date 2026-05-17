import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gsuid_core.aps import start_scheduler, shutdown_scheduler
from gsuid_core.logger import logger, clean_log
from gsuid_core.server import core_start_execute, core_shutdown_execute, core_start_before_execute
from gsuid_core.shutdown import shutdown_event


@asynccontextmanager
async def lifespan(app: FastAPI):
    from gsuid_core.webconsole import _setup_frontend
    from gsuid_core.utils.download_resource.download_core import check_speed

    # 先执行启动前钩子（数据库迁移、全局变量加载等），阻塞式
    await core_start_before_execute()

    asyncio.create_task(check_speed())
    asyncio.create_task(core_start_execute())

    async def _bg_setup_frontend():
        try:
            await _setup_frontend()
        except Exception as e:
            logger.exception(f"💻 [网页控制台] 后台初始化失败: {e}")

    asyncio.create_task(_bg_setup_frontend())

    await start_scheduler()

    # deprecate
    # await trans_global_val()

    asyncio.create_task(clean_log())

    yield

    logger.info("[GsCore] 开始关闭流程，设置 shutdown_event...")
    shutdown_event.set()

    await shutdown_scheduler()
    await core_shutdown_execute()


app = FastAPI(lifespan=lifespan)
