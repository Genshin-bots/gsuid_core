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
                for cfg_name in config_obj.config_default:
                    if cfg_name not in config_obj.config:
                        continue
                    config = config_obj.config[cfg_name]
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
            for key in config_obj.config_default:
                if key not in config_obj.config:
                    continue
                config = config_obj.config[key]
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


@app.post("/api/plugins/{plugin_name}")
async def update_plugin_config(request: Request, plugin_name: str, data: Dict, _user: Dict = Depends(require_auth)):
    """Update plugin configuration - supports multiple config objects per plugin"""
    name = plugin_name.lower()
    is_success = False

    # 检查是否包含新的 config_groups 格式
    if "config_groups" in data:
        # 处理新的 config_groups 格式
        for config_group in data["config_groups"]:
            config_name = config_group.get("config_name")
            group_config = config_group.get("config", {})

            # 查找对应的配置对象
            found_config = False
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
                    found_config = True

            # 如果没有找到现有配置，尝试动态创建配置对象
            if not found_config and config_name:
                try:
                    from gsuid_core.data_store import get_res_path
                    from gsuid_core.utils.plugins_config.models import (
                        GsStrConfig,
                        GsBoolConfig,
                        GsDictConfig,
                        GsListConfig,
                        GsListStrConfig,
                    )
                    from gsuid_core.utils.plugins_config.gs_config import StringConfig

                    # 构建默认配置
                    default_config = {
                        "Ann_Groups": GsDictConfig("推送公告群组", "原神公告推送群组", {}),
                        "Ann_Ids": GsListConfig("推送公告ID", "原神公告推送ID列表", []),
                        "SignTime": GsListStrConfig(
                            "每晚签到时间设置", "每晚米游社签到时间设置（时，分）", ["0", "38"]
                        ),
                        "BBSTaskTime": GsListStrConfig(
                            "每晚米游社任务时间设置", "每晚米游社任务时间设置（时，分）", ["1", "41"]
                        ),
                        "GetDrawTaskTime": GsListStrConfig(
                            "每晚留影叙佳期任务时间设置", "每晚留影叙佳期任务时间设置（时，分）", ["3", "25"]
                        ),
                        "MhyBBSCoinReport": GsBoolConfig(
                            "米游币推送", "开启后会私聊每个用户当前米游币任务完成情况", False
                        ),
                        "MhyBBSCoinReportGroup": GsBoolConfig(
                            "米游币群聊推送", "开启后会在群聊中推送当前群米游币任务完成情况", True
                        ),
                        "SignReportSimple": GsBoolConfig("简洁签到报告", "开启后可以大大减少每日签到报告字数", True),
                        "PrivateReport": GsBoolConfig(
                            "私聊报告", "关闭后将不再给主人推送当天米游币任务完成情况", False
                        ),
                        "PrivateSignReport": GsBoolConfig(
                            "签到私聊报告", "关闭后将不再给任何人推送当天签到任务完成情况", True
                        ),
                        "RandomPic": GsBoolConfig("随机图", "开启后[查询心海]等命令展示图将替换为随机图片", False),
                        "random_pic_API": GsStrConfig(
                            "随机图API",
                            "用于面板查询的随机图API",
                            "https://genshin-res.cherishmoon.fun/img?name=",
                            ["https://genshin-res.cherishmoon.fun/img?name="],
                        ),
                        "SchedSignin": GsBoolConfig("定时签到", "开启后每晚00:30将开始自动签到任务", True),
                        "SchedMhyBBSCoin": GsBoolConfig("定时米游币", "开启后每晚01:16将开始自动米游币任务", True),
                        "SchedGetDraw": GsBoolConfig("定时留影叙佳期", "开启后每晚03:25将开始自动米游币任务", True),
                        "SchedResinPush": GsBoolConfig(
                            "定时检查体力", "开启后每隔半小时检查一次开启推送的人的体力状态", True
                        ),
                        "CrazyNotice": GsBoolConfig("催命模式", "开启后当达到推送阈值将会一直推送", False),
                        "OldPanle": GsBoolConfig("旧面板", "会稍微增加面板访问速度,但会损失很多功能", False),
                        "ColorBG": GsBoolConfig("多彩面板", "面板颜色不按照属性来渲染,而按照自定义颜色", False),
                        "DefaultPayWX": GsBoolConfig(
                            "支付默认微信", "开启后使用gsrc命令将会以微信作为优先付款方式", False
                        ),
                        "DefaultBaseBG": GsBoolConfig("固定背景", "开启后部分功能的背景图将固定为特定背景", False),
                        "PicWiki": GsBoolConfig("图片版WIKI", "开启后支持的WIKI功能将转为图片版", True),
                        "WidgetResin": GsBoolConfig(
                            "体力使用组件API", "开启后mr功能将转为调用组件API, 可能缺失数据、数据不准", True
                        ),
                        "EnableAkasha": GsBoolConfig("排名系统", "开启后强制刷新将同时刷新AkashaSystem", False),
                        "help_column": GsStrConfig("帮助图列数", "修改帮助图有多少列", "6"),
                        "EnableCharCardByMys": GsBoolConfig(
                            "从米游社获取面板替代Enka服务", "开启后角色卡片将从米游社获取, 可能会遇到验证码", False
                        ),
                    }

                    # 创建配置路径
                    res_path = get_res_path()
                    config_path = res_path / "GenshinUID" / "config.json"

                    # 确保目录存在
                    config_path.parent.mkdir(parents=True, exist_ok=True)

                    # 创建配置对象
                    config_obj = StringConfig(config_name, config_path, default_config)

                    # 更新配置项
                    for key, value in group_config.items():
                        if key in config_obj.config_list:
                            config_obj.set_config(key, value)
                            is_success = True

                except Exception as e:
                    print(f"动态创建配置失败: {e}")
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
