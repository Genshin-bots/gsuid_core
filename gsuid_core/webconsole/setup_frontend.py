from fastapi.responses import FileResponse, HTMLResponse

from gsuid_core.logger import logger
from gsuid_core.data_store import DIST_PATH


async def _setup_frontend():
    """Setup frontend static files and API routes"""

    """确保webuser表存在"""
    try:
        from sqlmodel import SQLModel

        from gsuid_core.utils.database.base_models import engine

        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        logger.info("[WebUser] 数据库表创建成功!")
    except Exception as e:
        logger.warning(f"[WebUser] 数据库表创建失败: {e}")

    # 导入 app 对象和 web_api 模块
    # web_api 模块在导入时会自动注册所有路由到 app 对象
    from gsuid_core.webconsole.app_app import app

    # web_api 模块已自动将路由注册到 app，无需 include_router

    logger.info(f"💻 [网页控制台] 检查dist目录: {DIST_PATH}, 存在: {DIST_PATH.exists()}")

    # Mount static files if dist folder exists
    if DIST_PATH.exists():
        # 获取 HOST 和 PORT 配置
        from gsuid_core.config import core_config

        HOST = core_config.get_config("HOST")
        PORT = core_config.get_config("PORT")

        logger.info(f"💻 [网页控制台] 准备挂载前端到 /app, 目录: {DIST_PATH}")

        # 使用 APIRouter 来托管前端
        from fastapi import APIRouter

        router = APIRouter()

        @router.get("/")
        @router.get("/{path:path}")
        async def serve_frontend(path: str = ""):
            logger.info(f"💻 [网页控制台] 收到请求: /app/{path}")

            # 如果路径为空或只有 /，返回 index.html
            if not path or path == "/":
                index_path = DIST_PATH / "index.html"
                logger.info(f"💻 [网页控制台] 返回 index.html, 路径: {index_path}")
                if index_path.exists():
                    return FileResponse(index_path)

            # 尝试作为文件提供
            file_path = DIST_PATH / path
            logger.info(f"💻 [网页控制台] 尝试提供文件: {file_path}")
            if file_path.exists() and file_path.is_file():
                return FileResponse(file_path)

            # 对于 SPA，返回 index.html 让前端路由处理
            index_path = DIST_PATH / "index.html"
            logger.info("💻 [网页控制台] SPA fallback 返回 index.html")
            if index_path.exists():
                return FileResponse(index_path)

            return HTMLResponse("Not Found", status_code=404)

        # 注册路由，添加 /app 前缀
        app.include_router(router, prefix="/app")

        logger.info("💻 [网页控制台] 已通过 APIRouter 挂载前端到 /app")

        logger.info("💻 [网页控制台] 尝试挂载WebConsole")

        if HOST == "localhost" or HOST == "127.0.0.1":
            _host = "localhost"
            logger.warning("💻 WebConsole挂载于本地, 如想外网访问请修改data/config.json中host为0.0.0.0!")
        else:
            _host = HOST

        logger.success(f"💻 WebConsole挂载成功: http://{_host}:{PORT}/app")
    else:
        logger.warning(f"💻 [网页控制台] dist目录不存在 ({DIST_PATH}), 前端页面未挂载")
        logger.info("💻 请先构建前端: cd data-harmony-hub-main && npm run build")
