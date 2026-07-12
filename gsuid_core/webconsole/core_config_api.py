"""
Core Config APIs
提供核心配置相关的 RESTful APIs
"""

from typing import Any, Dict, List

from fastapi import Depends, Request

from gsuid_core.config import CONFIG_DEFAULT, CONFIG_OPTIONS, core_config
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

from ._api_tags import CORE_CONFIG


@app.get("/api/core/config", summary="获取核心配置", tags=CORE_CONFIG)
async def get_core_config(request: Request, _user: Dict[str, Any] = Depends(require_auth)):
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
        if key in ["sv"]:
            continue
        value = config.get(key)
        if value is not None:
            result[key] = value
        else:
            result[key] = CONFIG_DEFAULT[key]

    return {"status": 0, "msg": "ok", "data": result}


@app.get("/api/core/config/options", summary="获取核心配置项的可选值元数据", tags=CORE_CONFIG)
async def get_core_config_options(request: Request, _user: Dict[str, Any] = Depends(require_auth)):
    """获取核心配置中「枚举类」配置项的控件类型 + 可选值 + 展示标签。

    这些项在 config.py 的 CORE_CONFIG 里用 SelectOption 与默认值一处声明（如 LANGUAGE、
    嵌套的 log.level / log.output，嵌套 key 以 "_" 扁平化为 log_level 等）；
    新增此类「只能从固定集合选值」的配置只需在后端加一行 SelectOption，前端零改动自动渲染。
    """
    data = {key: opt.resolve() for key, opt in CONFIG_OPTIONS.items()}
    return {"status": 0, "msg": "ok", "data": data}


@app.post("/api/core/config", summary="保存核心配置", tags=CORE_CONFIG)
async def set_core_config(request: Request, data: Dict[str, Any], _user: Dict[str, Any] = Depends(require_auth)):
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
