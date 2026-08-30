import json
from typing import Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.data_store import DIST_PATH, DIST_EX_PATH


def parse_version(version_str: str) -> tuple[int, ...]:
    """解析版本号字符串为元组，支持0.0.0格式"""
    try:
        return tuple(int(x) for x in version_str.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def compare_versions(v1: Optional[dict], v2: Optional[dict]) -> int:
    """
    比较两个version.json的版本
    返回: 1表示v1更新, -1表示v2更新, 0表示相同或无效
    """
    if v1 is None and v2 is None:
        return 0
    if v1 is None:
        return -1
    if v2 is None:
        return 1

    v1_str = v1.get("version", "0.0.0")
    v2_str = v2.get("version", "0.0.0")

    v1_tuple = parse_version(v1_str)
    v2_tuple = parse_version(v2_str)

    # 补齐长度
    max_len = max(len(v1_tuple), len(v2_tuple))
    v1_tuple = v1_tuple + (0,) * (max_len - len(v1_tuple))
    v2_tuple = v2_tuple + (0,) * (max_len - len(v2_tuple))

    if v1_tuple > v2_tuple:
        return 1
    elif v1_tuple < v2_tuple:
        return -1
    return 0


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
        tool_outputs_api,
        ai_statistics_api,
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


async def setup_frontend_b() -> None:
    """Setup frontend static files and API routes"""

    """确保核心数据库表存在"""
    try:
        from gsuid_core.utils.database.startup import ensure_core_database_tables

        await ensure_core_database_tables()
    except Exception as e:
        logger.warning(t("log.webconsole.create_fail", e=e))

    # 导入 app 对象
    from gsuid_core.webconsole.app_app import app

    # 导入所有 webconsole API 模块以注册路由。
    try:
        _import_webconsole_apis()
    except Exception as e:
        logger.exception(t("log.webconsole.api_import_fail", e=e))

    dvj = DIST_PATH / "version.json"
    devj = DIST_EX_PATH / "version.json"

    dvj_version: Optional[dict] = None
    devj_version: Optional[dict] = None

    def get_version_str(v: Optional[dict]) -> str:
        """安全获取版本字符串"""
        return v.get("version", "unknown") if v else "unknown"

    # 读取 version.json 文件
    if dvj.exists():
        try:
            with open(dvj, "r", encoding="utf-8") as f:
                dvj_version = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    if devj.exists():
        try:
            with open(devj, "r", encoding="utf-8") as f:
                devj_version = json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    # 根据版本号比较选择使用哪个dist目录
    dist_ex_exists = DIST_EX_PATH.exists() and list(DIST_EX_PATH.iterdir())
    dist_exists = DIST_PATH.exists() and list(DIST_PATH.iterdir())

    # 默认使用 DIST_PATH
    dist_path = DIST_PATH

    if dist_ex_exists and dist_exists:
        # 两个目录都存在且非空，根据版本号选择
        cmp_result = compare_versions(devj_version, dvj_version)
        if cmp_result > 0:
            dist_path = DIST_EX_PATH
        elif cmp_result < 0:
            dist_path = DIST_PATH
        else:
            # 版本相同，优先使用 DIST_EX_PATH
            dist_path = DIST_PATH
    elif dist_ex_exists:
        # 只有 DIST_EX_PATH 存在
        dist_path = DIST_EX_PATH
    elif dist_exists:
        # 只有 DIST_PATH 存在
        dist_path = DIST_PATH
    else:
        # 两个目录都不存在或为空
        logger.warning(t("log.webconsole.dist_path_ex"))
        dist_path = DIST_PATH

    last_version = get_version_str(devj_version if dist_path == DIST_EX_PATH else dvj_version)
    # 最终结果日志
    logger.info(
        t(
            "log.webconsole.dist_path_last_version",
            dist_path=dist_path,
            last_version=last_version,
        )
    )

    # Mount static files if dist folder exists
    if dist_path.exists():
        # 获取 HOST 和 PORT 配置
        from gsuid_core.config import core_config

        HOST = core_config.get_config("HOST")
        PORT = core_config.get_config("PORT")

        logger.info(t("log.webconsole.app_dist_path", dist_path=dist_path))

        from gsuid_core.webconsole.static_serve import build_frontend_router

        app.include_router(build_frontend_router(dist_path), prefix="/app")

        logger.info(t("log.webconsole.apirouter_app"))

        logger.info(t("log.webconsole.webconsole"))

        if HOST == "localhost" or HOST == "127.0.0.1":
            _host = "localhost"
            logger.warning(t("log.webconsole.webconsole_data_config_json_host"))
        else:
            _host = HOST

        logger.success(t("log.webconsole.webconsole_http_host_port_app_ok", _host=_host, PORT=PORT))
    else:
        logger.warning(t("log.webconsole.dist_path", DIST_PATH=DIST_PATH))
