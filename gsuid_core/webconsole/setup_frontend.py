import json
from typing import Optional

from fastapi.responses import FileResponse, HTMLResponse

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
            dist_path = DIST_EX_PATH
    elif dist_ex_exists:
        # 只有 DIST_EX_PATH 存在
        dist_path = DIST_EX_PATH
    elif dist_exists:
        # 只有 DIST_PATH 存在
        dist_path = DIST_PATH
    else:
        # 两个目录都不存在或为空
        logger.warning("💻 [网页控制台] DIST_PATH 和 DIST_EX_PATH 都不存在或为空")
        dist_path = DIST_PATH

    last_version = get_version_str(devj_version if dist_path == DIST_EX_PATH else dvj_version)
    # 最终结果日志
    logger.info(f"💻 [网页控制台] 使用前端路径: {dist_path}, 版本: {last_version}")

    # Mount static files if dist folder exists
    if dist_path.exists():
        # 获取 HOST 和 PORT 配置
        from gsuid_core.config import core_config

        HOST = core_config.get_config("HOST")
        PORT = core_config.get_config("PORT")

        logger.info(f"💻 [网页控制台] 准备挂载前端到 /app, 目录: {dist_path}")

        # 使用 APIRouter 来托管前端
        from fastapi import APIRouter

        router = APIRouter()

        @router.get("/")
        @router.get("/{path:path}")
        async def serve_frontend(path: str = ""):
            logger.info(f"💻 [网页控制台] 收到请求: /app/{path}")

            # 如果路径为空或只有 /，返回 index.html
            if not path or path == "/":
                index_path = dist_path / "index.html"
                logger.info(f"💻 [网页控制台] 返回 index.html, 路径: {index_path}")
                if index_path.exists():
                    return FileResponse(index_path)

            # 尝试作为文件提供
            file_path = dist_path / path
            logger.info(f"💻 [网页控制台] 尝试提供文件: {file_path}")
            if file_path.exists() and file_path.is_file():
                return FileResponse(file_path)

            # 对于 SPA，返回 index.html 让前端路由处理
            index_path = dist_path / "index.html"
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
