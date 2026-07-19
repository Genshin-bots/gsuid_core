"""
Plugins APIs
提供插件管理相关的 RESTful APIs
"""

import base64
from typing import Any, Dict, Optional
from pathlib import Path

from fastapi import Body, Query, Depends, Request

from gsuid_core.sv import SL
from gsuid_core.i18n import t
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsDivider,
    GsIntConfig,
    GsStrConfig,
    GsColorConfig,
    GsFloatConfig,
    GsImageConfig,
    GsListStrConfig,
    GsFileUploadConfig,
    GsFilesUploadConfig,
    GsRepeatGroupConfig,
)
from gsuid_core.utils.plugins_update._plugins import PLUGINS_PATH, get_plugin_commit, get_local_plugins_list
from gsuid_core.utils.plugins_config.gs_config import all_config_list
from gsuid_core.utils.plugins_update.reload_plugin import reload_plugin

from ._api_tags import PLUGINS, FRAMEWORK_CONFIG

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


def _group_item_values(item: Dict[str, GSC]) -> Dict[str, Any]:
    """把一组 Dict[str, GSC] 抽成 {字段->值}(前端只需值); 嵌套组递归成值列表。"""
    out: Dict[str, Any] = {}
    for key, field in item.items():
        if isinstance(field, GsRepeatGroupConfig):
            out[key] = [_group_item_values(sub) for sub in field.data]
        elif isinstance(field, GsDivider):
            continue
        else:
            out[key] = field.data
    return out


def _build_config_item(config: GSC) -> Dict[str, Any]:
    """构建单个配置项的响应数据"""
    config_type = type(config).__name__.replace("Config", "").lower()

    # GsRepeatGroupConfig: template 给字段描述(递归 _build_config_item), value 给各项的值
    if isinstance(config, GsRepeatGroupConfig):
        return {
            "type": config_type,
            "title": config.title,
            "desc": config.desc,
            "value": [_group_item_values(row) for row in config.data],
            "default": [],
            "template": {k: _build_config_item(v) for k, v in config.template.items()},
        }

    # GsDivider 作为前端分割线，data 为可选标题
    if isinstance(config, GsDivider):
        divider: Dict[str, Any] = {
            "type": config_type,
            "title": config.title,
            "desc": config.desc,
        }
        if config.data is not None:
            divider["value"] = config.data
            divider["default"] = config.data
        return divider

    # 对于 GsImageConfig，文件不存在时返回空值
    value: Any = config.data
    if isinstance(config, GsImageConfig) and config.data:
        image_path = Path(config.data)
        if not image_path.exists() or not image_path.is_file():
            value = ""

    item: Dict[str, Any] = {
        "value": value,
        "default": config.data,
        "type": config_type,
        "title": config.title,
        "desc": config.desc,
    }

    # secret: 标记敏感项（前端应渲染为密码框）；GsDivider / GsColorConfig 无该字段
    if not isinstance(config, (GsDivider, GsColorConfig)) and config.secret:
        item["secret"] = True

    # options: 仅 GsStrConfig / GsListStrConfig / GsIntConfig 拥有
    if isinstance(config, (GsStrConfig, GsListStrConfig, GsIntConfig)) and config.options:
        item["options"] = config.options

    # regex: 仅 GsStrConfig（仅用于前端正则校验）
    if isinstance(config, GsStrConfig) and config.regex:
        item["regex"] = config.regex

    # 范围限制: GsIntConfig 仅 max_value, GsFloatConfig 含 min/max
    if isinstance(config, (GsIntConfig, GsFloatConfig)) and config.max_value is not None:
        item["max_value"] = config.max_value
    if isinstance(config, GsFloatConfig) and config.min_value is not None:
        item["min_value"] = config.min_value

    # 文件上传类: 传递 upload_to / filename / suffix
    if isinstance(config, (GsImageConfig, GsFileUploadConfig)):
        if config.upload_to:
            item["upload_to"] = str(config.upload_to) if isinstance(config.upload_to, Path) else config.upload_to
        if config.filename:
            item["filename"] = str(config.filename) if isinstance(config.filename, Path) else config.filename
        if config.suffix:
            item["suffix"] = config.suffix
    elif isinstance(config, GsFilesUploadConfig):
        if config.suffix:
            item["suffix"] = config.suffix

    return item


# ====================
# Plugin APIs
# ====================


@app.get("/api/plugins/list", summary="获取插件列表", tags=PLUGINS)
async def get_plugins_list(request: Request, _user: Dict[str, Any] = Depends(require_auth)):
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

        tasks.append(
            {
                "id": name,
                "name": plugin_name,
                "description": f"已加载插件：{plugin_name}",
                "enabled": plugin.enabled,
                "status": "running",
                "commit": get_plugin_commit(plugin_name),
            }
        )

    return {"status": 0, "msg": "ok", "data": tasks}


@app.get("/api/plugins/{plugin_name}", summary="获取插件详情", tags=PLUGINS)
async def get_plugin_detail(request: Request, plugin_name: str, _user: Dict[str, Any] = Depends(require_auth)):
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
            "commit": get_plugin_commit(actual_plugin_name),
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


@app.get("/api/framework-config/list", summary="获取框架配置列表", tags=FRAMEWORK_CONFIG)
async def get_framework_config_list(
    request: Request,
    prefix: str = Query(default="GsCore", description="配置名称前缀筛选，默认为 GsCore"),
    _user: Dict[str, Any] = Depends(require_auth),
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


@app.get("/api/framework-config/{config_name}", summary="获取框架配置详情", tags=FRAMEWORK_CONFIG)
async def get_framework_config_detail(
    request: Request, config_name: str, _user: Dict[str, Any] = Depends(require_auth)
):
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


@app.post("/api/framework-config/{config_name}", summary="更新框架配置", tags=FRAMEWORK_CONFIG)
async def update_framework_config(
    request: Request, config_name: str, data: Dict[str, Any] = Body(...), _user: Dict[str, Any] = Depends(require_auth)
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
                logger.warning(t("[框架配置][{config_name}] 配置项 {key} 写入失败", config_name=config_name, key=key))
        except Exception as e:
            logger.error(
                t("[框架配置][{config_name}] 配置项 {key} 写入异常: {e}", config_name=config_name, key=key, e=e)
            )

    if not is_success:
        return {"status": 1, "msg": "部分或全部配置项保存失败"}

    return {"status": 0, "msg": "配置已保存"}


@app.post("/api/framework-config/{config_name}/item/{item_name}", summary="更新框架配置项", tags=FRAMEWORK_CONFIG)
async def update_framework_config_item(
    request: Request,
    config_name: str,
    item_name: str,
    value: Any = Body(..., embed=True),
    _user: Dict[str, Any] = Depends(require_auth),
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
            logger.warning(
                t("[框架配置][{config_name}] 配置项 {item_name} 写入失败", config_name=config_name, item_name=item_name)
            )
            return {"status": 1, "msg": "配置项写入失败"}
    except Exception as e:
        logger.error(
            t(
                "[框架配置][{config_name}] 配置项 {item_name} 写入异常: {e}",
                config_name=config_name,
                item_name=item_name,
                e=e,
            )
        )
        return {"status": 1, "msg": f"配置项写入异常: {str(e)}"}


@app.post("/api/plugins/{plugin_name}", summary="更新插件配置", tags=PLUGINS)
async def update_plugin_config(
    request: Request, plugin_name: str, data: Dict[str, Any], _user: Dict[str, Any] = Depends(require_auth)
):
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


@app.post("/api/plugins/{plugin_name}/config/{config_name}/{item_name}", summary="更新单个配置项", tags=PLUGINS)
async def update_plugin_config_item(
    request: Request,
    plugin_name: str,
    config_name: str,
    item_name: str,
    value: Any = Body(...),
    _user: Dict[str, Any] = Depends(require_auth),
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


@app.post("/api/plugins/{plugin_name}/service", summary="更新插件服务配置", tags=PLUGINS)
async def update_plugin_service_config(
    request: Request, plugin_name: str, data: Dict[str, Any], _user: Dict[str, Any] = Depends(require_auth)
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


@app.post("/api/plugins/{plugin_name}/service/{field_name}", summary="更新插件服务字段", tags=PLUGINS)
async def update_plugin_service_field(
    request: Request,
    plugin_name: str,
    field_name: str,
    value: Any = Body(...),
    _user: Dict[str, Any] = Depends(require_auth),
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


@app.post("/api/plugins/{plugin_name}/sv/{sv_name}", summary="更新服务(SV)配置", tags=PLUGINS)
async def update_sv_config(
    request: Request,
    plugin_name: str,
    sv_name: str,
    data: Dict[str, Any],
    _user: Dict[str, Any] = Depends(require_auth),
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


@app.post("/api/plugins/{plugin_name}/sv/{sv_name}/{field_name}", summary="更新服务(SV)字段", tags=PLUGINS)
async def update_sv_field(
    request: Request,
    plugin_name: str,
    sv_name: str,
    field_name: str,
    value: Any = Body(...),
    _user: Dict[str, Any] = Depends(require_auth),
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


@app.post("/api/plugins/{plugin_name}/toggle", summary="切换插件开关", tags=PLUGINS)
async def toggle_plugin(
    request: Request, plugin_name: str, enabled: bool, _user: Dict[str, Any] = Depends(require_auth)
):
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


@app.post("/api/plugins/{plugin_name}/reload", summary="重新加载插件", tags=PLUGINS)
async def reload_plugin_api(request: Request, plugin_name: str, _user: Dict[str, Any] = Depends(require_auth)):
    """
    重新加载指定插件

    Args:
        request: FastAPI 请求对象
        plugin_name: 插件名称
        _user: 认证用户信息

    Returns:
        status: 0成功
        msg: 操作结果信息
    """
    result = reload_plugin(plugin_name)
    if result.lstrip().startswith("❌"):
        return {"status": 1, "msg": result}
    return {"status": 0, "msg": result}


# ===================
# Plugin Store APIs
# ===================


@app.get("/api/plugin-store/list", summary="获取插件商店列表", tags=PLUGINS)
async def get_plugin_store_list(request: Request, _user: Dict[str, Any] = Depends(require_auth)):
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

        logger.warning(t("log.webconsole.plugin_store_fail", error=e))
        return {"status": 1, "msg": f"获取插件列表失败: {str(e)}", "data": []}


@app.post("/api/plugin-store/install/{plugin_id}", summary="通过插件 ID 安装（插件商店白名单）", tags=PLUGINS)
async def install_plugin(
    request: Request, plugin_id: str, repo_url: str = Body(embed=True), _user: Dict[str, Any] = Depends(require_auth)
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
        from gsuid_core.utils.plugins_update._plugins import install_plugin as _install_plugin

        # install_plugin 内部已完成「安装 + reload_plugin 加载」，直接复用其返回信息，
        # 不再在此处重复 reload。失败信息统一以 ❌ 前缀标识。
        result = await _install_plugin(plugin_id)
        if result.lstrip().startswith("❌"):
            return {"status": 1, "msg": result}
        return {"status": 0, "msg": result}
    except Exception as e:
        return {"status": 1, "msg": f"安装失败: {str(e)}"}


@app.post("/api/plugin-store/install-url", summary="通过 URL 安装（任意 git 仓库） ⭐", tags=PLUGINS)
async def install_plugin_from_url_api(
    request: Request,
    data: Dict[str, Any] = Body(...),
    _user: Dict[str, Any] = Depends(require_auth),
):
    """
    从 URL 安装插件（不走插件商店白名单）

    接受前端传来的 git 仓库 URL，后端执行 ``git clone`` 把仓库克隆到
    ``plugins/`` 目录，并通过 ``reload_plugin`` 真正加载插件。

    适用场景：
    - 安装未收录在插件商店中的自定义插件 / 内部仓库 / 第三方仓库
    - 通过 URL 形式快速拉取指定插件

    与 ``POST /api/plugin-store/install/{plugin_id}`` 的区别：
    该接口接受 ``plugin_id``（必须在商店列表中）并由后端解析 URL；
    本接口接受原始 URL，由后端从 URL 末段推导插件目录名。

    Args:
        request: FastAPI 请求对象
        data: JSON body
            - ``url`` (str, 必填): git 仓库 URL，支持 http(s) / ssh(scp)，
              末尾 ``.git`` 后缀可选。
              例如 ``https://github.com/owner/MyPlugin.git``、
              ``git@github.com:owner/MyPlugin.git``。
            - ``branch`` (str, 可选): 指定克隆分支，默认使用仓库默认分支。
        _user: 认证用户信息

    Returns:
        status: 0 成功，1 失败
        msg: 操作结果信息（成功以 ✅ 开头，失败以 ❌ 开头，
             包含原始 git 错误信息，便于前端直接展示）

    请求示例:
        POST /api/plugin-store/install-url
        Content-Type: application/json
        {
            "url": "https://github.com/KimigaiiWuyi/GenshinUID.git"
        }

    响应示例（成功）:
        {
            "status": 0,
            "msg": "✅ 插件GenshinUID安装并加载成功!"
        }

    响应示例（已存在）:
        {
            "status": 1,
            "msg": "❌ 该插件已经安装过了!"
        }

    响应示例（URL 非法）:
        {
            "status": 1,
            "msg": "❌ URL 协议不支持, 当前仅支持 http://、https://、ssh://、git@ 开头的 git 仓库"
        }

    响应示例（网络/克隆失败）:
        {
            "status": 1,
            "msg": "❌ 插件MyPlugin安装失败: 克隆失败: ..."
        }
    """
    try:
        from gsuid_core.utils.plugins_update._plugins import install_plugin_from_url

        url = (data or {}).get("url")
        branch = (data or {}).get("branch")

        if not isinstance(url, str) or not url.strip():
            return {"status": 1, "msg": "❌ 请提供有效的 git 仓库 URL"}

        if branch is not None and not isinstance(branch, str):
            return {"status": 1, "msg": "❌ branch 字段必须是字符串"}

        # install_plugin_from_url 内部已完成「URL 校验 + clone + reload」，
        # 失败信息统一以 ❌ 前缀标识。
        result = await install_plugin_from_url(url, branch=branch)
        if result.lstrip().startswith("❌"):
            return {"status": 1, "msg": result}
        return {"status": 0, "msg": result}
    except Exception as e:
        return {"status": 1, "msg": f"安装失败: {str(e)}"}


@app.post("/api/plugin-store/update/{plugin_id}", summary="更新已安装插件", tags=PLUGINS)
async def update_plugin(request: Request, plugin_id: str, _user: Dict[str, Any] = Depends(require_auth)):
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


@app.delete("/api/plugin-store/uninstall/{plugin_id}", summary="卸载已安装插件", tags=PLUGINS)
async def uninstall_plugin(request: Request, plugin_id: str, _user: Dict[str, Any] = Depends(require_auth)):
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
