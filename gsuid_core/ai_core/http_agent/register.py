"""把 Agent 路由挂到主 FastAPI。AI 总开关关闭时不挂；Admin 建钥仍走核心 API。"""

from __future__ import annotations

from fastapi import FastAPI

from gsuid_core.i18n import t
from gsuid_core.logger import logger


def register_http_agent_routes(app: FastAPI) -> None:
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        logger.info(t("log.ai.http_agent_skip_ai_disabled"))
        return
    from gsuid_core.ai_core.http_agent.routes import agent_router

    app.include_router(agent_router)
