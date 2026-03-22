"""
Core Config APIs
提供核心配置相关的 RESTful APIs
"""

from typing import Dict, List

from fastapi import Depends, Request

from gsuid_core.config import CONFIG_DEFAULT, core_config
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth


@app.get("/api/core/config")
async def get_core_config(request: Request, _user: Dict = Depends(require_auth)):
    """
    获取核心配置

    返回 GsCore 的核心配置项。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 核心配置对象
    """
    config = core_config.config
    result = {}
    for key in CONFIG_DEFAULT:
        if key in ["sv", "plugins"]:
            continue
        value = config.get(key)
        if value is not None:
            result[key] = value
        else:
            result[key] = CONFIG_DEFAULT[key]

    return {"status": 0, "msg": "ok", "data": result}


@app.post("/api/core/config")
async def set_core_config(request: Request, data: Dict, _user: Dict = Depends(require_auth)):
    """
    保存核心配置

    Args:
        request: FastAPI 请求对象
        data: 配置项键值对
        _user: 认证用户信息

    Returns:
        status: 0成功
        msg: 配置保存结果信息
    """
    result = {}
    for i in data:
        if (i in CONFIG_DEFAULT and isinstance(CONFIG_DEFAULT[i], List)) or i in ["log_output"]:
            v = data[i].split(",") if isinstance(data[i], str) else data[i]
        else:
            v = data[i]

        if i in ["log_level", "log_output"]:
            g = i.split("_")
            k = g[0]
            if k not in result:
                result[k] = {}
            result[k][g[1]] = v
            continue

        core_config.lazy_set_config(i, v)

    for r in result:
        core_config.lazy_set_config(r, result[r])

    # 统一写入配置文件
    core_config.lazy_write_config()

    return {"status": 0, "msg": "配置已保存，重启后生效"}
