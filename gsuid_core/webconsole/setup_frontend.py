import asyncio
from pathlib import Path

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import DIST_PATH, DIST_EX_PATH


def _import_webconsole_apis() -> None:
    """导入 webconsole API 路由（WS 启动后后台调用，不阻塞启动）。

    核心 API 始终导入；AI API 会传递导入 pydantic_ai 等约 150MB 重栈，仅 AI 开启时导入。
    """
    # —— 核心 API（与 AI 无关，始终导入）——
    from gsuid_core.webconsole import (  # noqa: F401
        web_api,
        auth_api,
        logs_api,
        # 品牌信息 webconsole 后端
        brand_api,
        theme_api,
        # 追踪日志 webconsole 后端
        trace_api,
        assets_api,
        backup_api,
        system_api,
        history_api,
        message_api,
        plugins_api,
        version_api,
        database_api,
        dashboard_api,
        # 控制台 Live Chat 会话持久化
        live_chat_api,
        scheduler_api,
        git_mirror_api,
        git_update_api,
        http_trace_api,
        core_config_api,
        plugin_icon_api,
        plugin_page_api,
        http_agent_keys_api,
    )

    # —— AI API（拉起 AI 重依赖，仅在 AI 开启时导入）——
    from gsuid_core.ai_core.configs.ai_config import ai_config

    if not ai_config.get_config("enable").data:
        logger.info(t("log.webconsole.ai_api_mb_skip_import"))
        return

    from gsuid_core.webconsole import (  # noqa: F401
        meme_api,
        # AI 预算限制 webconsole 后端
        budget_api,
        # Agent Mesh Kanban webconsole 后端
        kanban_api,
        persona_api,
        ai_tools_api,
        ai_memory_api,
        ai_skills_api,
        ai_wizard_api,
        # 统一审批中心 webconsole 后端
        approvals_api,
        artifacts_api,
        image_rag_api,
        workspace_api,
        # Agent 套件槽位 / Hook 总线 / 关系温度的只读治理
        agent_kits_api,
        mcp_config_api,
        agent_debug_api,
        state_store_api,
        prompt_group_api,
        tool_outputs_api,
        ai_statistics_api,
        ai_memory_eval_api,
        ai_performance_api,
        knowledge_base_api,
        ai_session_logs_api,
        # 运维诊断中心（Bot/Session/触发回放/配置快照等）
        ops_diagnostics_api,
        provider_config_api,
        embedding_config_api,
        ai_scheduled_task_api,
        # 能力代理 webconsole 后端
        capability_agents_api,
        chat_with_history_api,
    )


def _announce_frontend(dist_path: Path) -> None:
    from gsuid_core.config import core_config

    host = core_config.get_config("HOST")
    port = core_config.get_config("PORT")
    logger.info(t("log.webconsole.app_dist_path", dist_path=dist_path))
    logger.info(t("log.webconsole.apirouter_app"))
    logger.info(t("log.webconsole.webconsole"))
    if host == "localhost" or host == "127.0.0.1":
        shown_host = "localhost"
        logger.warning(t("log.webconsole.webconsole_data_config_json_host"))
    else:
        shown_host = host
    logger.success(t("log.webconsole.webconsole_http_host_port_app_ok", _host=shown_host, PORT=port))


async def setup_frontend_b() -> None:
    """先挂本地较新的 dist，再在后台把资源站最高版本 zip 覆盖到 data/dist。"""
    from gsuid_core.webconsole.dist_sync import (
        version_label,
        probe_local_dists,
        select_frontend_dist,
        sync_remote_webconsole,
    )
    from gsuid_core.webconsole.static_serve import FrontendDist

    try:
        from gsuid_core.utils.database.startup import ensure_core_database_tables

        await ensure_core_database_tables()
    except Exception as e:
        logger.warning(t("log.webconsole.create_fail", e=e))

    try:
        _import_webconsole_apis()
    except Exception as e:
        logger.exception(t("log.webconsole.api_import_fail", e=e))

    local = await asyncio.to_thread(probe_local_dists, DIST_PATH, DIST_EX_PATH)
    if not local["bundled_ready"] and not local["extra_ready"]:
        logger.warning(t("log.webconsole.dist_path_ex"))

    dist_path = select_frontend_dist(DIST_PATH, DIST_EX_PATH, local)
    chosen = local["extra_version"] if dist_path == DIST_EX_PATH else local["bundled_version"]
    logger.info(
        t(
            "log.webconsole.dist_path_last_version",
            dist_path=dist_path,
            last_version=version_label(chosen),
        )
    )

    # 路由闭包读这个对象。下载完成后再改 path，不用重挂 /app。
    holder = FrontendDist(dist_path)
    from gsuid_core.webconsole.app_app import app
    from gsuid_core.webconsole.static_serve import (
        build_frontend_router,
        build_console_fonts_router,
    )

    mounted = False
    if dist_path.exists():
        app.include_router(build_console_fonts_router())
        app.include_router(build_frontend_router(holder), prefix="/app")
        _announce_frontend(dist_path)
        mounted = True
    else:
        logger.warning(t("log.webconsole.dist_path", DIST_PATH=dist_path))

    before = holder.path
    replaced = await sync_remote_webconsole(holder)
    if not mounted and holder.path.exists():
        app.include_router(build_console_fonts_router())
        app.include_router(build_frontend_router(holder), prefix="/app")
        _announce_frontend(holder.path)
    elif replaced and holder.path != before:
        fresh = await asyncio.to_thread(probe_local_dists, DIST_PATH, DIST_EX_PATH)
        current = fresh["extra_version"] if holder.path == DIST_EX_PATH else fresh["bundled_version"]
        logger.info(
            t(
                "log.webconsole.dist_switched",
                dist_path=holder.path,
                last_version=version_label(current),
            )
        )
