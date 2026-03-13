import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gsuid_core.aps import start_scheduler, shutdown_scheduler
from gsuid_core.logger import logger, clean_log
from gsuid_core.server import core_start_def, core_shutdown_def
from gsuid_core.utils.database.global_val_models import CoreDataAnalysis


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        logger.info(
            "♻ [GsCore] 执行启动Hook函数中！",
            [_def.__name__ for _def in core_start_def],
        )
        # 所有 startup 回调通过 create_task 在后台执行，框架启动不会被阻塞
        for _def in core_start_def:
            if asyncio.iscoroutinefunction(_def):
                asyncio.create_task(_def())
            else:
                asyncio.create_task(asyncio.to_thread(_def))
    except Exception as e:
        logger.exception(e)

    from gsuid_core.global_val import trans_global_val
    from gsuid_core.webconsole import _setup_frontend

    await _setup_frontend()
    await start_scheduler()
    asyncio.create_task(clean_log())
    await trans_global_val()

    # 将在几个版本后删除
    await CoreDataAnalysis.update_summary()

    yield

    await shutdown_scheduler()

    try:
        logger.info(
            "[GsCore] 执行关闭Hook函数中！",
            [_def.__name__ for _def in core_shutdown_def],
        )
        _task = [_def() for _def in core_shutdown_def]
        await asyncio.gather(*_task)
    except Exception as e:
        logger.exception(e)


app = FastAPI(lifespan=lifespan)
