# 导入 web_api 模块以注册所有路由
# 当其他模块导入 webconsole 时，会自动注册所有 API 路由
# 导出 mount_app 中的内容，保持向后兼容
from gsuid_core.webconsole import (
    web_api,
    auth_api,
    logs_api,
    mount_app,
    theme_api,
    assets_api,
    backup_api,
    system_api,
    history_api,
    message_api,
    persona_api,
    plugins_api,
    ai_tools_api,
    database_api,
    ai_skills_api,
    dashboard_api,
    image_rag_api,
    scheduler_api,
    setup_frontend,
    core_config_api,
    ai_statistics_api,
    system_prompt_api,
    knowledge_base_api,
    ai_scheduled_task_api,
)

# 导出 mount_app 中的常用对象
from gsuid_core.webconsole.mount_app import (
    PageSchema,
    GsAdminModel,
    site,
)
from gsuid_core.webconsole.setup_frontend import _setup_frontend

__all__ = [
    "web_api",
    "auth_api",
    "logs_api",
    "mount_app",
    "theme_api",
    "backup_api",
    "system_api",
    "plugins_api",
    "database_api",
    "ai_tools_api",
    "ai_skills_api",
    "dashboard_api",
    "image_rag_api",
    "scheduler_api",
    "setup_frontend",
    "core_config_api",
    "ai_statistics_api",
    "assets_api",
    "persona_api",
    "knowledge_base_api",
    "system_prompt_api",
    "ai_scheduled_task_api",
    "history_api",
    "PageSchema",
    "GsAdminModel",
    "site",
    "_setup_frontend",
    "message_api",
]
