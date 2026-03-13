"""
Plugins APIs
提供插件管理相关的 RESTful APIs
"""

import base64
from typing import Dict
from pathlib import Path

from fastapi import Body, Depends, Request

from gsuid_core.sv import SL
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.utils.plugins_update._plugins import PLUGINS_PATH, get_local_plugins_list
from gsuid_core.utils.plugins_config.gs_config import all_config_list


@app.get("/api/plugins")
async def get_plugins(request: Request, _user: Dict = Depends(require_auth)):
    """Get all loaded plugins with their config"""
    tasks = []

    # 只遍历已加载的插件 (SL.plugins)
    for plugin_name, plugin in SL.plugins.items():
        name = plugin_name.lower()

        # 读取插件图标
        icon_base64 = None
        icon_path = PLUGINS_PATH / plugin_name / "ICON.png"
        if not icon_path.exists():
            icon_path = PLUGINS_PATH / plugin_name.lower() / "ICON.png"
        if icon_path.exists() and icon_path.is_file():
            with open(icon_path, "rb") as f:
                icon_data = f.read()
                icon_base64 = f"data:image/png;base64,{base64.b64encode(icon_data).decode('utf-8')}"

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
                for cfg_name in config_obj.config_list:
                    config = config_obj.config_list[cfg_name]
                    config_type = type(config).__name__.replace("Config", "").lower()

                    item = {
                        "value": config.data,
                        "default": config.data,
                        "type": config_type,
                        "title": config.title,
                        "desc": config.desc,
                    }
                    options = getattr(config, "options", None)
                    if options:
                        item["options"] = options

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
                sv_list.append(
                    {
                        "name": sv.name,
                        "enabled": sv.enabled,
                        "pm": sv.pm,
                        "priority": sv.priority,
                        "area": sv.area,
                        "black_list": sv.black_list,
                        "white_list": sv.white_list,
                    }
                )

        sample = {
            "id": name,
            "name": plugin_name,
            "description": f"已加载插件：{plugin_name}",
            "enabled": plugin.enabled,
            "status": "running",
            "config": plugin_config,
            "config_groups": config_groups,
            "config_names": config_names,
            "service_config": service_config,
            "sv_list": sv_list,
            "icon": icon_base64,
        }
        tasks.append(sample)

    return {"status": 0, "msg": "ok", "data": tasks}


@app.get("/api/framework-config")
async def get_framework_config(request: Request, _user: Dict = Depends(require_auth)):
    """Get all GsCore framework configurations"""
    tasks = []

    # 遍历 all_config_list，查找所有 GsCore 开头的配置
    for config_name, config_obj in all_config_list.items():
        if config_name.startswith("GsCore"):
            # 构建配置对象
            config_data = {}
            for key in config_obj.config_list:
                config = config_obj.config_list[key]
                config_type = type(config).__name__.replace("Config", "").lower()
                config_data[key] = {
                    "value": config.data,
                    "default": config.data,
                    "type": config_type,
                    "title": config.title,
                    "desc": config.desc,
                }
                # 针对 GsImageConfig 传递额外参数
                if config_type == "gsimage":
                    for attr in ["upload_to", "filename", "suffix"]:
                        val = getattr(config, attr, None)
                        if val:
                            config_data[key][attr] = str(val) if isinstance(val, Path) else val

                options = getattr(config, "options", None)
                if options:
                    config_data[key]["options"] = options

            # 简化显示名称（去掉 GsCore 前缀）
            display_name = config_name[6:] if len(config_name) > 6 else "核心配置"

            tasks.append(
                {
                    "id": config_name,
                    "name": display_name,
                    "full_name": config_name,
                    "config": config_data,
                }
            )

    return {"status": 0, "msg": "ok", "data": tasks}


@app.post("/api/framework-config/{config_name}")
async def update_framework_config(request: Request, config_name: str, data: Dict, _user: Dict = Depends(require_auth)):
    """Update framework configuration"""
    # 完整配置名称
    full_config_name = config_name if config_name.startswith("GsCore") else f"GsCore{config_name}"

    if full_config_name not in all_config_list:
        return {"status": 1, "msg": "配置不存在"}

    config_obj = all_config_list[full_config_name]

    for key, value in data.items():
        config_obj.set_config(key, value)

    return {"status": 0, "msg": "配置已保存"}


@app.post("/api/plugins/{plugin_name}")
async def update_plugin_config(request: Request, plugin_name: str, data: Dict, _user: Dict = Depends(require_auth)):
    """Update plugin configuration - supports multiple config objects per plugin"""
    name = plugin_name.lower()
    is_success = False

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


@app.post("/api/plugins/{plugin_name}/service")
async def update_plugin_service_config(
    request: Request, plugin_name: str, data: Dict, _user: Dict = Depends(require_auth)
):
    """Update plugin service configuration - accepts modern list types directly"""
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


@app.post("/api/plugins/{plugin_name}/sv/{sv_name}")
async def update_sv_config(
    request: Request, plugin_name: str, sv_name: str, data: Dict, _user: Dict = Depends(require_auth)
):
    """Update individual service configuration - accepts modern list types directly"""
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


@app.post("/api/plugins/{plugin_name}/toggle")
async def toggle_plugin(request: Request, plugin_name: str, enabled: bool, _user: Dict = Depends(require_auth)):
    """Enable or disable a plugin"""
    # TODO: Implement actual enable/disable logic
    return {"status": 0, "msg": f"插件已{'启用' if enabled else '禁用'}"}


# ===================
# Plugin Store APIs
# ===================


@app.get("/api/plugin-store/list")
async def get_plugin_store_list(request: Request, _user: Dict = Depends(require_auth)):
    """Get list of available plugins from the remote store"""
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
    """Install a plugin from the store"""
    try:
        from gsuid_core.utils.plugins_update._plugins import install_plugin

        result = await install_plugin(plugin_id)

        if result:
            return {"status": 0, "msg": "插件安装成功"}
        else:
            return {"status": 1, "msg": "插件安装失败"}
    except Exception as e:
        return {"status": 1, "msg": f"安装失败: {str(e)}"}


@app.post("/api/plugin-store/update/{plugin_id}")
async def update_plugin(request: Request, plugin_id: str, _user: Dict = Depends(require_auth)):
    """Update an installed plugin"""
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
    """Uninstall an installed plugin"""
    try:
        from gsuid_core.utils.plugins_update._plugins import uninstall_plugin

        plugin_path = PLUGINS_PATH / plugin_id
        result = await uninstall_plugin(plugin_path)

        if result:
            return {"status": 0, "msg": "插件卸载成功"}
        else:
            return {"status": 1, "msg": "插件卸载失败"}
    except Exception as e:
        return {"status": 1, "msg": f"卸载失败: {str(e)}"}
