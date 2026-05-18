# webconsole 包初始化

# 现在 API 模块的导入与路由注册改由 setup_frontend.py 的 setup_frontend_b()
# 在 WS 服务启动后的后台阶段执行，本文件只保留轻量的 mount_app 再导出。
from gsuid_core.webconsole.mount_app import (
    PageSchema,
    GsAdminModel,
    site,
)
from gsuid_core.webconsole.setup_frontend import setup_frontend_b

__all__ = [
    "PageSchema",
    "GsAdminModel",
    "site",
    "setup_frontend_b",
]
