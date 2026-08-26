import os
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from gsuid_core.aps import start_scheduler, shutdown_scheduler
from gsuid_core.i18n import t
from gsuid_core.logger import logger, clean_trace_collector
from gsuid_core.server import core_start_execute, core_shutdown_execute, core_start_before_execute
from gsuid_core.shutdown import shutdown_event


@asynccontextmanager
async def lifespan(app: FastAPI):
    from gsuid_core.webconsole.setup_frontend import setup_frontend_b
    from gsuid_core.utils.download_resource.download_core import check_speed

    # 先执行启动前钩子（数据库迁移、全局变量加载等），阻塞式
    await core_start_before_execute()

    asyncio.create_task(check_speed())
    asyncio.create_task(core_start_execute())

    async def _bgsetup_frontend_b():
        try:
            await setup_frontend_b()
        except Exception as e:
            logger.exception(t("log.core.web_console_background_initialization_fail", e=e))

    asyncio.create_task(_bgsetup_frontend_b())

    await start_scheduler()

    # deprecate
    # await trans_global_val()

    # 日志缓冲不再需要周期性清空：内存已由 LOG_HISTORY_MAXLEN + LOG_HISTORY_MAX_CHARS
    # 在 append 时按需淘汰保证有界；清空只会抹掉网页控制台的回放积压（见 logger.clean_log）
    asyncio.create_task(clean_trace_collector())

    yield

    logger.info(t("log.core.gscore_shutdown_process_setting_start"))
    shutdown_event.set()

    await shutdown_scheduler()
    await core_shutdown_execute()


_ENABLE_OPENAPI = os.getenv("GSUID_ENABLE_OPENAPI", "").lower() in ("1", "true", "yes")
app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if _ENABLE_OPENAPI else None,
    redoc_url="/redoc" if _ENABLE_OPENAPI else None,
    openapi_url="/openapi.json" if _ENABLE_OPENAPI else None,
)

from gsuid_core.ai_core.http_agent.register import register_http_agent_routes  # noqa: E402

register_http_agent_routes(app)
