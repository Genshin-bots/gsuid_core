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
    """Get all plugins with their databases"""
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
    """Get tables for a specific plugin"""
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
    """Get table metadata including columns"""
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
    """Get paginated data from table with optional search and filter"""
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
    """Create a new record"""
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
    """Update a record"""
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
    """Delete a record"""
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
