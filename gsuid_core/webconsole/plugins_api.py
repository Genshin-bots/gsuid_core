"""
Plugins APIs
提供插件管理相关的 RESTful APIs
"""

import base64
from typing import Any, Dict, Optional
from pathlib import Path

from fastapi import Body, Query, Depends, Request

from gsuid_core.sv import SL
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.plugins_update._plugins import PLUGINS_PATH, get_local_plugins_list
from gsuid_core.utils.plugins_config.gs_config import all_config_list

# ====================
# 辅助函数
# ====================


def _read_plugin_icon(plugin_name: str) -> Optional[str]:
    """读取插件图标，返回 base64 编码的字符串"""
    icon_base64 = None
    icon_path = PLUGINS_PATH / plugin_name / "ICON.png"
    if not icon_path.exists():
        icon_path = PLUGINS_PATH / plugin_name.lower() / "ICON.png"
    if icon_path.exists() and icon_path.is_file():
        with open(icon_path, "rb") as f:
            icon_data = f.read()
            icon_base64 = f"data:image/png;base64,{base64.b64encode(icon_data).decode('utf-8')}"
    return icon_base64


def _build_config_item(config) -> Dict:
    """构建单个配置项的响应数据"""
    config_type = type(config).__name__.replace("Config", "").lower()

    value = config.data
    # 对于GsImage类型，检查文件是否存在，如果不存在则返回空值
    if config_type == "gsimage" and isinstance(value, str) and value:
        image_path = Path(value)
        if not image_path.exists() or not image_path.is_file():
            value = ""

    item = {
        "value": value,
        "default": config.data,
        "type": config_type,
        "title": config.title,
        "desc": config.desc,
    }
    options = getattr(config, "options", None)
    if options:
        item["options"] = options

    # 针对 GsImageConfig 传递额外参数
    if config_type == "gsimage":
        for attr in ["upload_to", "filename", "suffix"]:
            val = getattr(config, attr, None)
            if val:
                item[attr] = str(val) if isinstance(val, Path) else val

    return item


# ====================
# Plugin APIs
# ====================


@app.get("/api/plugins/list")
async def get_plugins_list(request: Request, _user: Dict = Depends(require_auth)):
    """
    获取所有已加载插件的列表（轻量级接口）

    返回所有已加载插件的基本信息，包含 ICON 头像数据。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 插件列表，每项包含 id、name、description、enabled、status、icon
    """
    tasks = []

    for plugin_name, plugin in SL.plugins.items():
        name = plugin_name.lower()
        icon_base64 = _read_plugin_icon(plugin_name)

        tasks.append(
            {
                "id": name,
                "name": plugin_name,
                "description": f"已加载插件：{plugin_name}",
                "enabled": plugin.enabled,
                "status": "running",
                "icon": icon_base64,
            }
        )

    return {"status": 0, "msg": "ok", "data": tasks}


@app.get("/api/plugins/{plugin_name}")
async def get_plugin_detail(request: Request, plugin_name: str, _user: Dict = Depends(require_auth)):
    """
    获取单个插件的完整信息

    返回插件的详细信息，包括配置、服务、图标等。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称
        _user: 认证用户信息

    Returns:
        status: 0成功，1插件不存在
        data: 插件详细信息对象

    前端调用方式:
        GET /api/plugins/{plugin_name}

    返回 Sample:
        {
            "status": 0,
            "msg": "ok",
            "data": {
                "id": "gsuid_core",
                "name": "gsuid_core",
                "description": "已加载插件：gsuid_core",
                "enabled": true,
                "status": "running",
                "config": {...},
                "config_groups": [...],
                "config_names": [...],
                "service_config": {
                    "enabled": true,
                    "pm": 6,
                    "priority": 5,
                    "area": "ALL",
                    "black_list": [],
                    "white_list": [],
                    "prefix": [],
                    "force_prefix": [],
                    "disable_force_prefix": false,
                    "allow_empty_prefix": true
                },
                "sv_list": [
                    {
                        "name": "帮助",
                        "enabled": true,
                        "pm": 6,
                        "priority": 5,
                        "area": "GROUP",
                        "black_list": [],
                        "white_list": [],
                        "commands": [
                            {
                                "type": "command",
                                "keyword": "帮助",
                                "block": false,
                                "to_me": false
                            },
                            {
                                "type": "prefix",
                                "keyword": "我的",
                                "block": false,
                                "to_me": false
                            },
                            {
                                "type": "keyword",
                                "keyword": "原石",
                                "block": false,
                                "to_me": false
                            },
                            {
                                "type": "regex",
                                "keyword": ".*原石.*",
                                "block": false,
                                "to_me": false
                            }
                        ]
                    }
                ],
                "icon": "base64_encoded_icon_data"
            }
        }

        其中 sv_list 中的 commands 字段说明:
            - type: 触发器类型，可选值: "command"(命令), "prefix"(前缀匹配), "suffix"(后缀匹配),
                    "keyword"(关键字匹配), "fullmatch"(完全匹配), "regex"(正则匹配), "file"(文件类型), "message"(消息)
            - keyword: 触发关键字/正则表达式
            - block: 是否阻止后续触发
            - to_me: 是否仅响应 @ 机器人
    """
    name = plugin_name.lower()

    # 查找插件
    plugin = None
    actual_plugin_name = plugin_name
    for key in SL.plugins.keys():
        if key.lower() == name:
            plugin = SL.plugins[key]
            actual_plugin_name = key
            break

    if not plugin:
        return {"status": 1, "msg": "插件不存在"}

    # 读取插件图标
    icon_base64 = _read_plugin_icon(actual_plugin_name)

    # Get plugin config if exists - collect all related configs by plugin_name
    plugin_config = {}
    config_groups = []
    config_names = []

    # 通过 plugin_name 属性查找该插件的所有配置
    for config_key, config_obj in all_config_list.items():
        # 获取配置对象的 plugin_name 属性
        config_plugin_name = getattr(config_obj, "plugin_name", None)

        # 如果配置属于这个插件（通过 plugin_name 匹配）
        if config_plugin_name and config_plugin_name.lower() == name.lower():
            config_names.append(config_key)
            group_config = {}
            for cfg_name in config_obj.config_default:
                if cfg_name not in config_obj.config:
                    continue
                config = config_obj.config[cfg_name]
                item = _build_config_item(config)

                group_config[cfg_name] = item
                # 保持平铺结构兼容旧前端
                plugin_config[cfg_name] = item

            config_groups.append({"config_name": config_key, "config": group_config})

    # 获取插件服务配置
    service_config = {
        "enabled": plugin.enabled,
        "pm": plugin.pm,
        "priority": plugin.priority,
        "area": plugin.area,
        "black_list": plugin.black_list,
        "white_list": plugin.white_list,
        "prefix": plugin.prefix,
        "force_prefix": plugin.force_prefix,
        "disable_force_prefix": plugin.disable_force_prefix,
        "allow_empty_prefix": plugin.allow_empty_prefix,
    }

    # 获取插件下的单个服务配置 (从 SL.detail_lst)
    sv_list = []
    if plugin in SL.detail_lst:
        for sv in SL.detail_lst[plugin]:
            # 获取该服务下的所有触发器
            commands = []
            for trigger_type, triggers_dict in sv.TL.items():
                for cmd_key, trigger in triggers_dict.items():
                    commands.append(
                        {
                            "type": trigger.type,
                            "keyword": trigger.keyword,
                            "block": trigger.block,
                            "to_me": trigger.to_me,
                        }
                    )

            sv_list.append(
                {
                    "name": sv.name,
                    "enabled": sv.enabled,
                    "pm": sv.pm,
                    "priority": sv.priority,
                    "area": sv.area,
                    "black_list": sv.black_list,
                    "white_list": sv.white_list,
                    "commands": commands,
                }
            )

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "id": name,
            "name": actual_plugin_name,
            "description": f"已加载插件：{actual_plugin_name}",
            "enabled": plugin.enabled,
            "status": "running",
            "config": plugin_config,
            "config_groups": config_groups,
            "config_names": config_names,
            "service_config": service_config,
            "sv_list": sv_list,
            "icon": icon_base64,
        },
    }


# ====================
# Framework Config APIs
# ====================


@app.get("/api/framework-config/list")
async def get_framework_config_list(
    request: Request,
    prefix: str = Query(default="GsCore", description="配置名称前缀筛选，默认为 GsCore"),
    _user: Dict = Depends(require_auth),
):
    """
    获取所有框架配置的列表（轻量级接口）

    返回所有可用的框架配置项基本信息。

    Args:
        request: FastAPI 请求对象
        prefix: 配置名称前缀筛选，默认为 GsCore
        _user: 认证用户信息

    Returns:
        status: 0成功
        data: 配置列表，每项包含 id、name、full_name
    """
    tasks = []

    for config_name, config_obj in all_config_list.items():
        if config_name.startswith(prefix):
            # 简化显示名称（去掉前缀）
            display_name = config_name[len(prefix) :] if len(config_name) > len(prefix) else "核心配置"

            tasks.append(
                {
                    "id": config_name,
                    "name": display_name,
                    "full_name": config_name,
                }
            )

    return {"status": 0, "msg": "ok", "data": tasks}


@app.get("/api/framework-config/{config_name}")
async def get_framework_config_detail(request: Request, config_name: str, _user: Dict = Depends(require_auth)):
    """
    获取单个框架配置的完整信息

    返回框架配置的详细结构，包含所有配置项。

    Args:
        request: FastAPI 请求对象
        config_name: 配置名称
        _user: 认证用户信息

    Returns:
        status: 0成功，1配置不存在
        data: 配置详细信息
    """
    # 完整配置名称
    full_config_name = config_name if config_name.startswith("GsCore") else f"GsCore{config_name}"

    if full_config_name not in all_config_list:
        return {"status": 1, "msg": "配置不存在"}

    config_obj = all_config_list[full_config_name]

    # 构建配置对象
    config_data = {}
    for key in config_obj.config_default:
        if key not in config_obj.config:
            continue
        config = config_obj.config[key]
        item = _build_config_item(config)
        config_data[key] = item

    # 简化显示名称（去掉 GsCore 前缀）
    display_name = full_config_name[6:] if len(full_config_name) > 6 else "核心配置"

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "id": full_config_name,
            "name": display_name,
            "full_name": full_config_name,
            "config": config_data,
        },
    }


@app.post("/api/framework-config/{config_name}")
async def update_framework_config(
    request: Request, config_name: str, data: Dict = Body(...), _user: Dict = Depends(require_auth)
):
    """
    更新框架配置

    批量更新指定配置项的值。

    Args:
        request: FastAPI 请求对象
        config_name: 配置名称
        data: 配置项键值对
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    from gsuid_core.logger import logger

    # 完整配置名称
    full_config_name = config_name if config_name.startswith("GsCore") else f"GsCore{config_name}"

    if full_config_name not in all_config_list:
        return {"status": 1, "msg": "配置不存在"}

    config_obj = all_config_list[full_config_name]
    is_success = False

    for key, value in data.items():
        try:
            result = config_obj.set_config(key, value)
            if result:
                is_success = True
            else:
                logger.warning(f"[框架配置][{config_name}] 配置项 {key} 写入失败")
        except Exception as e:
            logger.error(f"[框架配置][{config_name}] 配置项 {key} 写入异常: {e}")

    if not is_success:
        return {"status": 1, "msg": "部分或全部配置项保存失败"}

    return {"status": 0, "msg": "配置已保存"}


@app.post("/api/framework-config/{config_name}/item/{item_name}")
async def update_framework_config_item(
    request: Request,
    config_name: str,
    item_name: str,
    value: Any = Body(...),
    _user: Dict = Depends(require_auth),
):
    """
    更新单个框架配置项

    更新指定配置对象中的单个配置项值。

    Args:
        request: FastAPI 请求对象
        config_name: 配置名称
        item_name: 配置项名称
        value: 配置项新值
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    from gsuid_core.logger import logger

    # 完整配置名称
    full_config_name = config_name if config_name.startswith("GsCore") else f"GsCore{config_name}"

    if full_config_name not in all_config_list:
        return {"status": 1, "msg": "配置不存在"}

    config_obj = all_config_list[full_config_name]

    if item_name not in config_obj.config_list:
        return {"status": 1, "msg": f"配置项 {item_name} 不存在"}

    try:
        result = config_obj.set_config(item_name, value)
        if result:
            return {"status": 0, "msg": "配置项已保存"}
        else:
            logger.warning(f"[框架配置][{config_name}] 配置项 {item_name} 写入失败")
            return {"status": 1, "msg": "配置项写入失败"}
    except Exception as e:
        logger.error(f"[框架配置][{config_name}] 配置项 {item_name} 写入异常: {e}")
        return {"status": 1, "msg": f"配置项写入异常: {str(e)}"}


@app.post("/api/plugins/{plugin_name}")
async def update_plugin_config(request: Request, plugin_name: str, data: Dict, _user: Dict = Depends(require_auth)):
    """
    更新插件配置

    支持多配置对象格式和传统平铺格式。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称
        data: 配置数据（支持 config_groups 格式或平铺格式）
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    name = plugin_name.lower()
    is_success = False

    # 检查是否包含新的 config_groups 格式
    if "config_groups" in data:
        # 处理新的 config_groups 格式
        for config_group in data["config_groups"]:
            config_name = config_group.get("config_name")
            group_config = config_group.get("config", {})

            for cfg_key, config_obj in all_config_list.items():
                config_plugin_name = getattr(config_obj, "plugin_name", None)

                # 匹配插件名或配置组名
                if (config_plugin_name and config_plugin_name.lower() == name) or (
                    config_name and cfg_key.lower() == config_name.lower()
                ):
                    # 更新该组的配置项
                    for key, value in group_config.items():
                        if key in config_obj.config_list:
                            config_obj.set_config(key, value)
                            is_success = True
    else:
        # 原有的平铺格式处理
        # 查找该插件的所有配置对象
        for config_key, config_obj in all_config_list.items():
            config_plugin_name = getattr(config_obj, "plugin_name", None)
            # 匹配插件名
            if config_plugin_name and config_plugin_name.lower() == name:
                # 遍历提交的数据，尝试更新匹配的配置项
                for key, value in data.items():
                    # 处理带前缀的 key: {config_key}_{cfg_name}
                    actual_key = key
                    prefix = f"{config_key}_"
                    if key.startswith(prefix):
                        actual_key = key[len(prefix) :]

                    if actual_key in config_obj.config_list:
                        config_obj.set_config(actual_key, value)
                        is_success = True

            # 兼容旧逻辑：如果 config_key 直接匹配 plugin_name
            elif config_key.lower() in [name, name.rstrip("uid")]:
                for key, value in data.items():
                    if key in config_obj.config_list:
                        config_obj.set_config(key, value)
                        is_success = True

    if not is_success:
        return {"status": 1, "msg": "未找到可更新的配置项"}

    return {"status": 0, "msg": "配置已保存"}


@app.post("/api/plugins/{plugin_name}/config/{config_name}/{item_name}")
async def update_plugin_config_item(
    request: Request,
    plugin_name: str,
    config_name: str,
    item_name: str,
    value: Any = Body(...),
    _user: Dict = Depends(require_auth),
):
    """
    更新单个插件配置项

    更新指定插件配置中的单个配置项值。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称
        config_name: 配置名称
        item_name: 配置项名称
        value: 配置项新值
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    name = plugin_name.lower()

    # 查找该插件的配置对象
    for cfg_key, config_obj in all_config_list.items():
        config_plugin_name = getattr(config_obj, "plugin_name", None)

        # 匹配插件名或配置组名
        if (config_plugin_name and config_plugin_name.lower() == name) or (
            config_name and cfg_key.lower() == config_name.lower()
        ):
            if item_name in config_obj.config_list:
                try:
                    config_obj.set_config(item_name, value)
                    return {"status": 0, "msg": "配置项已保存"}
                except Exception as e:
                    return {"status": 1, "msg": f"配置项写入异常: {str(e)}"}

    return {"status": 1, "msg": "未找到可更新的配置项"}


@app.post("/api/plugins/{plugin_name}/service")
async def update_plugin_service_config(
    request: Request, plugin_name: str, data: Dict, _user: Dict = Depends(require_auth)
):
    """
    更新插件服务配置

    更新插件的服务级配置，如 pm、priority、area 等。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称
        data: 服务配置数据
        _user: 认证用户信息

    Returns:
        status: 0成功，1插件不存在
        msg: 操作结果信息
    """
    # Try multiple possible keys for the plugin
    plugin = None
    possible_plugin_keys = [
        plugin_name,
        plugin_name.lower(),
        plugin_name.rstrip("UID"),
        plugin_name.lower().rstrip("uid"),
    ]

    for key in possible_plugin_keys:
        if key in SL.plugins:
            plugin = SL.plugins[key]
            break

    if not plugin:
        return {"status": 1, "msg": "插件不存在"}

    # Convert data types as needed
    if "pm" in data:
        data["pm"] = int(data["pm"])
    if "priority" in data:
        data["priority"] = int(data["priority"])

    # Ensure lists are properly handled (no need for split, accept directly)
    plugin.set(False, **data)

    return {"status": 0, "msg": "服务配置已保存"}


@app.post("/api/plugins/{plugin_name}/service/{field_name}")
async def update_plugin_service_field(
    request: Request,
    plugin_name: str,
    field_name: str,
    value: Any = Body(...),
    _user: Dict = Depends(require_auth),
):
    """
    更新插件服务配置的单个字段

    更新指定插件服务配置中的单个字段值。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称
        field_name: 字段名称
        value: 字段新值
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    # Try multiple possible keys for the plugin
    plugin = None
    possible_plugin_keys = [
        plugin_name,
        plugin_name.lower(),
        plugin_name.rstrip("UID"),
        plugin_name.lower().rstrip("uid"),
    ]

    for key in possible_plugin_keys:
        if key in SL.plugins:
            plugin = SL.plugins[key]
            break

    if not plugin:
        return {"status": 1, "msg": "插件不存在"}

    # 允许更新的字段
    allowed_fields = [
        "pm",
        "priority",
        "area",
        "black_list",
        "white_list",
        "prefix",
        "force_prefix",
        "disable_force_prefix",
        "allow_empty_prefix",
        "enabled",
    ]

    if field_name not in allowed_fields:
        return {"status": 1, "msg": f"不允许更新字段 {field_name}"}

    # 类型转换
    if field_name in ["pm", "priority"]:
        value = int(value)

    plugin.set(False, **{field_name: value})
    return {"status": 0, "msg": "服务配置已保存"}


@app.post("/api/plugins/{plugin_name}/sv/{sv_name}")
async def update_sv_config(
    request: Request, plugin_name: str, sv_name: str, data: Dict, _user: Dict = Depends(require_auth)
):
    """
    更新单个服务配置

    更新指定服务的配置项。

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称
        sv_name: 服务名称
        data: 服务配置数据
        _user: 认证用户信息

    Returns:
        status: 0成功，1服务不存在
        msg: 操作结果信息
    """
    if sv_name not in SL.lst:
        return {"status": 1, "msg": "服务不存在"}

    sv = SL.lst[sv_name]

    # Convert data types as needed
    update_data = data.copy()
    if "pm" in update_data:
        update_data["pm"] = int(update_data["pm"])
    if "priority" in update_data:
        update_data["priority"] = int(update_data["priority"])

    sv.set(False, **update_data)

    return {"status": 0, "msg": "服务配置已保存"}


@app.post("/api/plugins/{plugin_name}/sv/{sv_name}/{field_name}")
async def update_sv_field(
    request: Request,
    plugin_name: str,
    sv_name: str,
    field_name: str,
    value: Any = Body(...),
    _user: Dict = Depends(require_auth),
):
    """
    更新单个服务配置的单个字段

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称
        sv_name: 服务名称
        field_name: 字段名称
        value: 字段新值
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    if sv_name not in SL.lst:
        return {"status": 1, "msg": "服务不存在"}

    sv = SL.lst[sv_name]

    # 允许更新的字段
    allowed_fields = ["pm", "priority", "area", "black_list", "white_list", "enabled"]

    if field_name not in allowed_fields:
        return {"status": 1, "msg": f"不允许更新字段 {field_name}"}

    # 类型转换
    if field_name in ["pm", "priority"]:
        value = int(value)

    sv.set(False, **{field_name: value})
    return {"status": 0, "msg": "服务配置已保存"}


@app.post("/api/plugins/{plugin_name}/toggle")
async def toggle_plugin(request: Request, plugin_name: str, enabled: bool, _user: Dict = Depends(require_auth)):
    """
    启用或禁用插件

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称
        enabled: 是否启用
        _user: 认证用户信息

    Returns:
        status: 0成功
        msg: 操作结果信息
    """
    # TODO: Implement actual enable/disable logic
    return {"status": 0, "msg": f"插件已{'启用' if enabled else '禁用'}"}


# ===================
# Plugin Store APIs
# ===================


@app.get("/api/plugin-store/list")
async def get_plugin_store_list(request: Request, _user: Dict = Depends(require_auth)):
    """
    获取远程插件商店的插件列表

    从远程服务器获取可用插件列表，并与本地已安装插件进行比对。

    Args:
        request: FastAPI 请求对象
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        data: 包含 plugins、fun_plugins、tool_plugins 的对象
    """
    import httpx

    from gsuid_core.utils.plugins_update.api import plugins_lib

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(plugins_lib, timeout=15)
            response.raise_for_status()
            plugin_data = response.json()

        # Get plugins data structure
        plugins_dict = plugin_data.get("plugins", {})

        # Get installed plugins
        installed_plugins = await get_local_plugins_list()
        installed_ids = set(installed_plugins.keys())

        # Format plugin data for frontend
        formatted_plugins = []

        for plugin_id, plugin_info in plugins_dict.items():
            is_installed = plugin_id.lower() in installed_ids

            # Determine status
            status = "not_installed"
            if is_installed:
                status = "installed"

            formatted_plugins.append(
                {
                    "id": plugin_id,
                    "name": plugin_info.get("info", plugin_id),
                    "description": plugin_info.get("info", ""),
                    "version": "latest",  # Remote version would need git check
                    "author": plugin_info.get("link", "").split("/")[-2] if plugin_info.get("link") else "Unknown",
                    "tags": plugin_info.get("alias", []),
                    "icon": plugin_info.get("icon", ""),
                    "cover": plugin_info.get("cover", ""),
                    "avatar": plugin_info.get("avatar", ""),
                    "link": plugin_info.get("link", ""),
                    "branch": plugin_info.get("branch", "main"),
                    "type": plugin_info.get("type", "tip"),
                    "content": plugin_info.get("content", "普通"),
                    "info": plugin_info.get("info", ""),
                    "installMsg": plugin_info.get("installMsg", ""),
                    "alias": plugin_info.get("alias", []),
                    "installed": is_installed,
                    "hasUpdate": False,  # Would need git version check
                    "status": status,
                }
            )

        # Get fun_plugins and tool_plugins from the remote data
        fun_plugins = plugin_data.get("fun_plugins", [])
        tool_plugins = plugin_data.get("tool_plugins", [])

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "plugins": formatted_plugins,
                "fun_plugins": fun_plugins,
                "tool_plugins": tool_plugins,
            },
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.warning(f"Failed to fetch plugin store list: {e}")
        return {"status": 1, "msg": f"获取插件列表失败: {str(e)}", "data": []}


@app.post("/api/plugin-store/install/{plugin_id}")
async def install_plugin(
    request: Request, plugin_id: str, repo_url: str = Body(embed=True), _user: Dict = Depends(require_auth)
):
    """
    从商店安装插件

    Args:
        request: FastAPI 请求对象
        plugin_id: 插件 ID
        repo_url: 插件仓库 URL
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    try:
        from gsuid_core.utils.plugins_update._plugins import install_plugin

        result = await install_plugin(plugin_id)

        if result == 0:
            return {"status": 0, "msg": "插件安装成功"}
        else:
            return {"status": 1, "msg": "插件安装失败"}
    except Exception as e:
        return {"status": 1, "msg": f"安装失败: {str(e)}"}


@app.post("/api/plugin-store/update/{plugin_id}")
async def update_plugin(request: Request, plugin_id: str, _user: Dict = Depends(require_auth)):
    """
    更新已安装的插件

    Args:
        request: FastAPI 请求对象
        plugin_id: 插件 ID
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    try:
        from gsuid_core.utils.plugins_update._plugins import update_plugins

        result = await update_plugins(plugin_id)

        if result:
            return {"status": 0, "msg": "插件更新成功"}
        else:
            return {"status": 1, "msg": "插件更新失败"}
    except Exception as e:
        return {"status": 1, "msg": f"更新失败: {str(e)}"}


@app.delete("/api/plugin-store/uninstall/{plugin_id}")
async def uninstall_plugin(request: Request, plugin_id: str, _user: Dict = Depends(require_auth)):
    """
    卸载已安装的插件

    Args:
        request: FastAPI 请求对象
        plugin_id: 插件 ID
        _user: 认证用户信息

    Returns:
        status: 0成功，1失败
        msg: 操作结果信息
    """
    try:
        from gsuid_core.utils.plugins_update._plugins import uninstall_plugin

        plugin_path = PLUGINS_PATH / plugin_id
        result = await uninstall_plugin(plugin_path)

        # 检查结果中是否包含失败标记
        if "❌" in result:
            return {"status": 1, "msg": result}
        else:
            return {"status": 0, "msg": result}
    except Exception as e:
        return {"status": 1, "msg": f"卸载失败: {str(e)}"}
