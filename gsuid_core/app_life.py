import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gsuid_core.aps import start_scheduler, shutdown_scheduler
from gsuid_core.logger import logger, clean_log
from gsuid_core.server import core_start_execute, core_shutdown_execute
from gsuid_core.shutdown import shutdown_event


@asynccontextmanager
async def lifespan(app: FastAPI):
    from gsuid_core.global_val import trans_global_val
    from gsuid_core.webconsole import _setup_frontend

    await core_start_execute()
    await _setup_frontend()
    await start_scheduler()
    await trans_global_val()

    asyncio.create_task(clean_log())

    yield

    logger.info("[GsCore] 开始关闭流程，设置 shutdown_event...")
    shutdown_event.set()

    await shutdown_scheduler()
    await core_shutdown_execute()


app = FastAPI(lifespan=lifespan)
