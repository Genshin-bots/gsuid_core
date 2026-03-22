"""
Database APIs
提供数据库管理相关的 RESTful APIs
"""

import base64
from typing import Any, Dict

from fastapi import Body, Depends, Request

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.database.admin_api import (
    create_record,
    delete_record,
    update_record,
    get_table_data,
    get_table_info,
    get_plugin_databases,
    get_all_plugin_databases,
)
from gsuid_core.utils.plugins_update._plugins import PLUGINS_PATH


@app.get("/api/database/plugins")
async def get_database_plugins(request: Request, _user: Dict = Depends(require_auth)):
    """
    获取所有插件及其数据库信息

    返回所有使用数据库的插件列表及其图标。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        data: 插件数据库信息列表
    """
    try:
        plugins = get_all_plugin_databases()

        # 为每个插件添加图标
        for plugin in plugins:
            # 读取插件图标
            icon_base64 = None
            plugin_dirs_to_check = [
                PLUGINS_PATH / plugin.plugin_id,
                PLUGINS_PATH / plugin.plugin_id.lower(),
                PLUGINS_PATH / plugin.plugin_id.rstrip("UID"),
                PLUGINS_PATH / plugin.plugin_id.lower().rstrip("uid"),
            ]

            for plugin_dir in plugin_dirs_to_check:
                if plugin_dir.exists() and plugin_dir.is_dir():
                    icon_path = plugin_dir / "ICON.png"
                    if icon_path.exists() and icon_path.is_file():
                        with open(icon_path, "rb") as f:
                            icon_data = f.read()
                            icon_base64 = f"data:image/png;base64,{base64.b64encode(icon_data).decode('utf-8')}"
                        break

            plugin.icon = icon_base64

        return {
            "status": 0,
            "msg": "ok",
            "data": [p.to_dict() for p in plugins],
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.error(f"Failed to get database plugins: {e}")
        return {"status": 1, "msg": str(e), "data": []}


@app.get("/api/database/{plugin_id}/tables")
async def get_plugin_tables(plugin_id: str, request: Request, _user: Dict = Depends(require_auth)):
    """
    获取指定插件的数据库表列表

    Args:
        plugin_id: 插件 ID
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功，1插件不存在
        data: 插件表信息
    """
    try:
        plugin = get_plugin_databases(plugin_id)
        if not plugin:
            return {"status": 1, "msg": f"Plugin {plugin_id} not found", "data": []}
        return {
            "status": 0,
            "msg": "ok",
            "data": plugin.to_dict(),
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.error(f"Failed to get plugin tables: {e}")
        return {"status": 1, "msg": str(e), "data": {}}


@app.get("/api/database/table/{table_name}")
async def get_table_metadata(table_name: str, request: Request, _user: Dict = Depends(require_auth)):
    """
    获取数据表元数据

    返回表的结构信息，包括列定义等。

    Args:
        table_name: 表名
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功，1表不存在
        data: 表元数据
    """
    try:
        table_info = get_table_info(table_name)
        if not table_info:
            return {"status": 1, "msg": f"Table {table_name} not found", "data": None}
        return {
            "status": 0,
            "msg": "ok",
            "data": table_info.to_dict(),
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.error(f"Failed to get table metadata: {e}")
        return {"status": 1, "msg": str(e), "data": None}


@app.get("/api/database/table/{table_name}/data")
async def get_table_data_api(
    table_name: str,
    page: int = 1,
    per_page: int = 20,
    search: str = "",
    search_columns: str = "",
    filter_columns: str = "",
    filter_values: str = "",
    _user: Dict = Depends(require_auth),
):
    """
    获取数据表分页数据

    支持搜索和过滤功能。

    Args:
        table_name: 表名
        page: 页码，默认1
        per_page: 每页数量，默认20
        search: 搜索关键字
        search_columns: 搜索列（逗号分隔）
        filter_columns: 过滤列（逗号分隔）
        filter_values: 过滤值（逗号分隔）
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 包含 items、total、page、per_page 的分页对象
    """
    try:
        result = await get_table_data(
            table_name,
            page=page,
            per_page=per_page,
            search=search,
            search_columns=search_columns,
            filter_columns=filter_columns,
            filter_values=filter_values,
        )
        return {
            "status": 0,
            "msg": "ok",
            "data": result.to_dict(),
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.error(f"Failed to get table data: {e}")
        return {"status": 1, "msg": str(e), "data": {"items": [], "total": 0, "page": page, "per_page": per_page}}


@app.post("/api/database/table/{table_name}/data")
async def create_record_api(
    table_name: str,
    data: Dict = Body(...),
    _user: Dict = Depends(require_auth),
):
    """
    创建新记录

    Args:
        table_name: 表名
        data: 要创建的记录数据
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        data: 创建的记录
    """
    try:
        record = await create_record(table_name, data)
        return {
            "status": 0,
            "msg": "创建成功",
            "data": record,
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.error(f"Failed to create record: {e}")
        return {"status": 1, "msg": str(e), "data": None}


@app.put("/api/database/table/{table_name}/data/{record_id}")
async def update_record_api(
    table_name: str,
    record_id: str,
    data: Dict = Body(...),
    _user: Dict = Depends(require_auth),
):
    """
    更新记录

    Args:
        table_name: 表名
        record_id: 记录 ID
        data: 要更新的字段数据
        _user: 认证用户信息

    Returns:
        status: 0成功，1记录不存在
        data: 更新后的记录
    """
    try:
        # Try to convert to int if it's numeric
        parsed_id: Any = record_id
        try:
            parsed_id = int(record_id)
        except (ValueError, TypeError):
            pass

        record = await update_record(table_name, parsed_id, data)
        if record is None:
            return {"status": 1, "msg": "记录不存在", "data": None}
        return {
            "status": 0,
            "msg": "更新成功",
            "data": record,
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.error(f"Failed to update record: {e}")
        return {"status": 1, "msg": str(e), "data": None}


@app.delete("/api/database/table/{table_name}/data/{record_id}")
async def delete_record_api(
    table_name: str,
    record_id: str,
    _user: Dict = Depends(require_auth),
):
    """
    删除记录

    Args:
        table_name: 表名
        record_id: 记录 ID
        _user: 认证用户信息

    Returns:
        status: 0成功，1记录不存在
        msg: 操作结果信息
    """
    try:
        # Try to convert to int if it's numeric
        parsed_id: Any = record_id
        try:
            parsed_id = int(record_id)
        except (ValueError, TypeError):
            pass

        success = await delete_record(table_name, parsed_id)
        if not success:
            return {"status": 1, "msg": "记录不存在", "data": None}
        return {
            "status": 0,
            "msg": "删除成功",
            "data": None,
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.error(f"Failed to delete record: {e}")
        return {"status": 1, "msg": str(e), "data": None}
