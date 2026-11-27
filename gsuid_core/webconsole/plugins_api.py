"""
Plugins APIs
提供插件管理相关的 RESTful APIs
"""

import base64
import hashlib
from typing import Any, Dict, Optional, TypedDict
from pathlib import Path

from fastapi import Body, File, Query, Depends, Request, UploadFile
from pydantic import BaseModel

from gsuid_core.sv import SL
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.meta_plugins import provides_of
from gsuid_core.plugin_registry import (
    SSH_PREFIX,
    LocalPluginMeta,
    LocalPluginDiskViewDict,
    plugin_registry,
    meta_to_disk_view,
    resolve_plugin_on_disk,
    is_builtin_mirror_or_proxy,
    normalize_mirror_config_pair,
    validate_mirror_prefix_async,
)
from gsuid_core.utils.secret_mask import (
    unmask_against,
    is_secret_key_name,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, require_admin, require_admin_header
from gsuid_core.webconsole.plugin_page import pages_for_plugin
from gsuid_core.webconsole.plugin_prefs import (
    PluginPrefsPayload,
    get_plugin_prefs,
    record_recent_plugin,
)
from gsuid_core.webconsole.session_store import SessionRecord
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GsDivider,
    GsIntConfig,
    GsStrConfig,
    GsColorConfig,
    GsFloatConfig,
    GsImageConfig,
    GsButtonConfig,
    GsListStrConfig,
    GsFileUploadConfig,
    GsFilesUploadConfig,
    GsRepeatGroupConfig,
    button_state_view,
)
from gsuid_core.utils.plugins_update._plugins import PLUGINS_PATH, get_plugin_commit, get_local_plugins_list
from gsuid_core.utils.plugins_config.gs_config import all_config_list
from gsuid_core.utils.plugins_update.reload_plugin import reload_plugin_async

from ._api_tags import PLUGINS, FRAMEWORK_CONFIG

# --- 辅助函数 ---


def _read_plugin_icon(plugin_name: str) -> Optional[str]:
    """读取插件图标，返回 base64 编码的字符串"""
    icon_base64 = None
    from gsuid_core.utils.path_safety import PathEscapeError, safe_join, is_safe_filename

    cached = plugin_registry.get(plugin_name, include_core=True, include_buildin=True)
    icon_path = cached.icon_path if cached is not None else None
    if (icon_path is None or not icon_path.exists()) and is_safe_filename(plugin_name):
        try:
            icon_path = safe_join(PLUGINS_PATH, plugin_name, "ICON.png")
        except PathEscapeError:
            icon_path = None
    if icon_path is None or not icon_path.exists():
        if is_safe_filename(plugin_name.lower()):
            try:
                icon_path = safe_join(PLUGINS_PATH, plugin_name.lower(), "ICON.png")
            except PathEscapeError:
                icon_path = None
        else:
            icon_path = None
    if icon_path is not None and icon_path.exists() and icon_path.is_file():
        with open(icon_path, "rb") as f:
            icon_data = f.read()
            icon_base64 = f"data:image/png;base64,{base64.b64encode(icon_data).decode('utf-8')}"
    return icon_base64


def _is_secret_gsc(config: GSC) -> bool:
    if isinstance(config, (GsDivider, GsColorConfig, GsButtonConfig)):
        return False
    return bool(config.secret)


def _write_config_item(config_obj: Any, key: str, value: Any) -> bool:
    """写入配置项；掩码回传按元素合并旧密钥，避免 tags 追加失败。"""
    if key not in config_obj.config_list:
        return False
    old = config_obj.config[key].data if key in config_obj.config else None
    return bool(config_obj.set_config(key, unmask_against(value, old)))


def _group_item_values(item: Dict[str, GSC]) -> Dict[str, Any]:
    """把一组 Dict[str, GSC] 抽成 {字段->值}(前端只需值); 嵌套组递归成值列表。"""
    out: Dict[str, Any] = {}
    for key, field in item.items():
        if isinstance(field, GsRepeatGroupConfig):
            out[key] = [_group_item_values(sub) for sub in field.data]
        elif isinstance(field, GsDivider):
            continue
        elif isinstance(field, GsButtonConfig):
            out[key] = button_state_view(field.data)
        else:
            out[key] = field.data
    return out


def _build_config_item(config: GSC, key: str = "") -> Dict[str, Any]:
    """构建单个配置项的响应数据。``key`` 用于按字段名识别密钥。"""
    config_type = type(config).__name__.replace("Config", "").lower()

    # GsRepeatGroupConfig: template 给字段描述(递归 _build_config_item), value 给各项的值
    if isinstance(config, GsRepeatGroupConfig):
        return {
            "type": config_type,
            "title": config.title,
            "desc": config.desc,
            "value": [_group_item_values(row) for row in config.data],
            "default": [],
            "template": {k: _build_config_item(v, k) for k, v in config.template.items()},
        }

    if isinstance(config, GsButtonConfig):
        state = button_state_view(config.data)
        return {
            "type": config_type,
            "title": config.title,
            "desc": config.desc,
            "value": state,
            "default": state,
            "endpoint": config.endpoint,
            "label": config.label,
            "level": config.level,
            "confirm": config.confirm,
            "status_fields": [{"key": row.key, "label": row.label} for row in config.status_fields],
            "show_progress": config.show_progress,
            "poll_ms": config.poll_ms,
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

    # secret: 标记敏感项（前端视觉隐藏）；已登录管理员 GET 下发明文。
    if _is_secret_gsc(config) or is_secret_key_name(key):
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


# --- Plugin APIs ---


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
    from gsuid_core.server import get_plugin_import_memory

    mem_map = get_plugin_import_memory()
    tasks = []

    for plugin_name, plugin in SL.plugins.items():
        name = plugin_name.lower()

        provides = provides_of(plugin_name)
        meta = plugin_registry.get(plugin_name, include_core=True, include_buildin=True)
        git = meta.git if meta is not None else None
        tasks.append(
            {
                "id": name,
                "name": plugin_name,
                "description": f"已加载插件：{plugin_name}",
                "enabled": plugin.enabled,
                "status": "running",
                "commit": get_plugin_commit(plugin_name),
                "kind": "meta" if provides is not None else "plugin",
                "provides": provides,
                "pages": pages_for_plugin(plugin_name),
                "memory_mb": mem_map[plugin_name] if plugin_name in mem_map else None,
                "is_git": bool(meta.is_git and meta.has_remote) if meta is not None else False,
                "source_kind": meta.source_kind if meta is not None else "local",
                "upstream_host": git.upstream_host if git is not None else "",
                "upstream_url": git.upstream_url if git is not None else "",
                "layout": meta.layout if meta is not None else "unknown",
            }
        )

    return {"status": 0, "msg": "ok", "data": tasks}


class _LocalPluginsDiskResp(TypedDict):
    status: int
    msg: str
    data: list[LocalPluginDiskViewDict]


class _RefreshPluginsData(TypedDict):
    total: int
    git_count: int
    local_count: int
    disabled_count: int
    failed_count: int


class _RefreshPluginsResp(TypedDict):
    status: int
    msg: str
    data: _RefreshPluginsData


@app.get("/api/plugins/local", summary="获取磁盘全量本地插件元信息", tags=PLUGINS)
async def get_local_plugins_disk_api(
    fresh: bool = Query(default=False, description="是否现扫磁盘刷新"),
    _user: SessionRecord = Depends(require_auth),
) -> _LocalPluginsDiskResp:
    """返回磁盘上全部用户插件（含未加载 / 加载失败条目），供本地插件管理与卸载使用。"""
    entries = await plugin_registry.list_plugins(scope="plugin", include_disabled=True, fresh=fresh, with_commit=False)
    return {
        "status": 0,
        "msg": "ok",
        "data": [meta_to_disk_view(item) for item in entries],
    }


@app.post("/api/plugins/refresh", summary="手动刷新本地插件元信息中心", tags=PLUGINS)
async def refresh_plugins_registry_api(
    _user: SessionRecord = Depends(require_admin),
) -> _RefreshPluginsResp:
    """管理员触发全量磁盘与 Git 元信息重扫（single-flight 合并，不阻塞 WS）。"""
    await plugin_registry.refresh_all(with_commit=True)
    entries = plugin_registry.list_cached(scope="plugin", include_disabled=True)
    git_count = sum(1 for item in entries if item.is_git and item.has_remote)
    disabled_count = sum(1 for item in entries if item.disabled)
    failed_count = sum(1 for item in entries if item.load_status == "failed")
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "total": len(entries),
            "git_count": git_count,
            "local_count": len(entries) - git_count,
            "disabled_count": disabled_count,
            "failed_count": failed_count,
        },
    }


@app.get("/api/plugins/usage", summary="插件命令触发热度", tags=PLUGINS)
async def get_plugin_usage_rank(_user: Dict[str, Any] = Depends(require_auth)):
    """近 7 天命令触发次数，按插件汇总。结果缓存 10 分钟，不扫消息流水。"""
    from gsuid_core.webconsole.plugin_usage import get_plugin_usage

    data = await get_plugin_usage()
    return {"status": 0, "msg": "ok", "data": data}


class RecentPluginBody(BaseModel):
    plugin: str
    edited: bool = False


class PluginPrefsApiResponse(TypedDict):
    status: int
    msg: str
    data: PluginPrefsPayload


@app.get("/api/plugins/recent", summary="获取最近使用与最后编辑的插件", tags=PLUGINS)
async def get_recent_plugins_api(
    _user: SessionRecord = Depends(require_auth),
) -> PluginPrefsApiResponse:
    """获取面板记录的最近使用插件列表（MRU）与最后编辑插件名。"""
    data = await get_plugin_prefs()
    return {"status": 0, "msg": "ok", "data": data}


@app.post("/api/plugins/recent", summary="记录最近使用或最后编辑的插件", tags=PLUGINS)
async def record_recent_plugin_api(
    body: RecentPluginBody,
    _user: SessionRecord = Depends(require_auth),
) -> PluginPrefsApiResponse:
    """把指定插件置顶到最近使用列表；``edited=True`` 时同步更新最后编辑插件。"""
    data = await record_recent_plugin(body.plugin, body.edited)
    return {"status": 0, "msg": "ok", "data": data}


@app.get("/api/plugins/{plugin_name}", summary="获取插件详情", tags=PLUGINS)
async def get_plugin_detail(request: Request, plugin_name: str, _user: Dict[str, Any] = Depends(require_admin_header)):
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
    meta_provides = provides_of(actual_plugin_name)

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
                item = _build_config_item(config, cfg_name)

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
        "bot_white_list": plugin.bot_white_list,
        "bot_black_list": plugin.bot_black_list,
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

    detail_meta = plugin_registry.get(actual_plugin_name, include_core=True, include_buildin=True)
    detail_git = detail_meta.git if detail_meta is not None else None
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
            "kind": "meta" if meta_provides is not None else "plugin",
            "provides": meta_provides,
            "is_git": bool(detail_meta.is_git and detail_meta.has_remote) if detail_meta is not None else False,
            "source_kind": detail_meta.source_kind if detail_meta is not None else "local",
            "upstream_host": detail_git.upstream_host if detail_git is not None else "",
            "upstream_url": detail_git.upstream_url if detail_git is not None else "",
            "layout": detail_meta.layout if detail_meta is not None else "unknown",
            "config": plugin_config,
            "config_groups": config_groups,
            "config_names": config_names,
            "service_config": service_config,
            "sv_list": sv_list,
            "icon": icon_base64,
            "pages": pages_for_plugin(actual_plugin_name),
        },
    }


# --- Framework Config APIs ---


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
    request: Request, config_name: str, _user: Dict[str, Any] = Depends(require_admin_header)
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
        item = _build_config_item(config, key)
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


async def _validate_git_mirror_config_write(
    full_config_name: str,
    key: str,
    value: object,
) -> tuple[str | None, object, bool]:
    """校验通过框架配置接口写入的 GitMirror（防 SSRF），并判断内置源是否需归位模式。"""
    if full_config_name != "GsCore" or key != "GitMirror":
        return None, value, False
    if not isinstance(value, str):
        return "镜像地址必须为字符串", value, False
    clean = value.strip()
    if clean and not clean.lower().startswith(SSH_PREFIX):
        ssrf_err = await validate_mirror_prefix_async(clean)
        if ssrf_err is not None:
            return f"镜像地址非法: {ssrf_err}", value, False
    norm_prefix, _ = normalize_mirror_config_pair(clean, "auto")
    # 写入空串 / ssh:// / 内置已知前缀时同步归位 GitMirrorMode，防止旧模式污染
    is_builtin_or_empty = norm_prefix in ("", SSH_PREFIX) or is_builtin_mirror_or_proxy(norm_prefix)
    return None, norm_prefix, is_builtin_or_empty


@app.post("/api/framework-config/{config_name}", summary="更新框架配置", tags=FRAMEWORK_CONFIG)
async def update_framework_config(
    request: Request,
    config_name: str,
    data: Dict[str, Any] = Body(...),
    _user: SessionRecord = Depends(require_admin),
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
    normalized_data: Dict[str, object] = {}
    reset_mirror_mode = False
    for key, value in data.items():
        err, norm_val, should_reset_mode = await _validate_git_mirror_config_write(full_config_name, key, value)
        if err is not None:
            return {"status": 1, "msg": err}
        normalized_data[key] = norm_val
        if should_reset_mode:
            reset_mirror_mode = True
    if reset_mirror_mode and "GitMirrorMode" in config_obj.config_list:
        normalized_data["GitMirrorMode"] = "auto"

    is_success = False

    for key, value in normalized_data.items():
        try:
            result = _write_config_item(config_obj, key, value)
            if result:
                is_success = True
            else:
                logger.warning(t("log.webconsole.config_name_key_write_fail", config_name=config_name, key=key))
        except Exception as e:
            logger.error(t("log.webconsole.config_name_key_write_exception", config_name=config_name, key=key, e=e))

    if not is_success:
        return {"status": 1, "msg": "部分或全部配置项保存失败"}

    return {"status": 0, "msg": "配置已保存"}


@app.post("/api/framework-config/{config_name}/item/{item_name}", summary="更新框架配置项", tags=FRAMEWORK_CONFIG)
async def update_framework_config_item(
    request: Request,
    config_name: str,
    item_name: str,
    value: Any = Body(..., embed=True),
    _user: SessionRecord = Depends(require_admin),
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

    err, norm_value, should_reset_mode = await _validate_git_mirror_config_write(full_config_name, item_name, value)
    if err is not None:
        return {"status": 1, "msg": err}

    try:
        result = _write_config_item(config_obj, item_name, norm_value)
        if result:
            if should_reset_mode and "GitMirrorMode" in config_obj.config_list:
                _write_config_item(config_obj, "GitMirrorMode", "auto")
            return {"status": 0, "msg": "配置项已保存"}
        else:
            logger.warning(
                t("log.webconsole.config_name_item_write_fail", config_name=config_name, item_name=item_name)
            )
            return {"status": 1, "msg": "配置项写入失败"}
    except Exception as e:
        logger.error(
            t(
                "log.webconsole.framework_config_name_writing_fail",
                config_name=config_name,
                item_name=item_name,
                e=e,
            )
        )
        return {"status": 1, "msg": f"配置项写入异常: {str(e)}"}


@app.post("/api/plugins/{plugin_name}", summary="更新插件配置", tags=PLUGINS)
async def update_plugin_config(
    request: Request, plugin_name: str, data: Dict[str, Any], _user: Dict[str, Any] = Depends(require_admin)
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
                        if _write_config_item(config_obj, key, value):
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

                    if _write_config_item(config_obj, actual_key, value):
                        is_success = True

            # 兼容旧逻辑：如果 config_key 直接匹配 plugin_name
            elif config_key.lower() in [name, name.rstrip("uid")]:
                for key, value in data.items():
                    if _write_config_item(config_obj, key, value):
                        is_success = True

    if not is_success:
        return {"status": 1, "msg": "未找到可更新的配置项"}

    await record_recent_plugin(plugin_name, True)
    return {"status": 0, "msg": "配置已保存"}


@app.post("/api/plugins/{plugin_name}/config/{config_name}/{item_name}", summary="更新单个配置项", tags=PLUGINS)
async def update_plugin_config_item(
    request: Request,
    plugin_name: str,
    config_name: str,
    item_name: str,
    value: Any = Body(...),
    _user: Dict[str, Any] = Depends(require_admin),
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
                    if _write_config_item(config_obj, item_name, value):
                        await record_recent_plugin(plugin_name, True)
                        return {"status": 0, "msg": "配置项已保存"}
                    return {"status": 1, "msg": "配置项写入失败"}
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
    await record_recent_plugin(plugin_name, True)

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
        "bot_white_list",
        "bot_black_list",
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
    await record_recent_plugin(plugin_name, True)
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
    await record_recent_plugin(plugin_name, True)

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
    await record_recent_plugin(plugin_name, True)
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
async def reload_plugin_api(request: Request, plugin_name: str, _user: Dict[str, Any] = Depends(require_admin)):
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
    result = await reload_plugin_async(plugin_name)
    # 始终带 data 字段：前端 api.post 会解包 data；无 data 时成功路径拿到 undefined，
    # 再读 result.status 会报 Cannot read properties of undefined (reading 'status')。
    if result.lstrip().startswith("❌"):
        return {"status": 1, "msg": result, "data": {"plugin_name": plugin_name, "ok": False}}
    await record_recent_plugin(plugin_name, True)
    return {"status": 0, "msg": result, "data": {"plugin_name": plugin_name, "ok": True}}


# --- Plugin Store APIs ---


def _plugin_store_order_key(
    plugin_id: str,
    *,
    deprecated: bool,
    has_update: bool,
    installed_tab: bool,
    order_seed: str | None,
) -> tuple[int, int, str]:
    random_key = ""
    if order_seed is not None:
        random_key = hashlib.sha256(f"{order_seed}\0{plugin_id}".encode()).hexdigest()
    return (int(deprecated), int(installed_tab and not has_update), random_key)


def _plugin_store_star_order_key(
    plugin_id: str,
    *,
    deprecated: bool,
    star_count: int | None,
    descending: bool,
    order_seed: str | None,
) -> tuple[int, int, int, str, str]:
    random_key = ""
    if order_seed is not None:
        random_key = hashlib.sha256(f"{order_seed}\0{plugin_id}".encode()).hexdigest()
    star_order = 0
    if star_count is not None:
        star_order = -star_count if descending else star_count
    return (int(deprecated), int(star_count is None), star_order, random_key, plugin_id.lower())


def _build_local_only_store_entry(
    plugin_key: str, raw_local: object, meta: LocalPluginMeta | None
) -> dict[str, object]:
    """把不在远程商店里的本地磁盘插件构造为商店列表兼容条目，用于「已安装」页签展示与卸载。"""
    display_name = meta.name if meta is not None else plugin_key
    if meta is None and isinstance(raw_local, dict) and "name" in raw_local and isinstance(raw_local["name"], str):
        display_name = raw_local["name"]
    git = meta.git if meta is not None else None
    upstream_url = git.upstream_url if git is not None else ""
    upstream_host = git.upstream_host if git is not None else ""
    author = git.upstream_owner if git is not None and git.upstream_owner else ""
    branch = git.branch if git is not None and git.branch else "main"
    source_kind = meta.source_kind if meta is not None else "local"
    load_status = meta.load_status if meta is not None else "unloaded"
    last_error = meta.last_error if meta is not None else None
    is_git = bool(meta.is_git and meta.has_remote) if meta is not None else False
    info_text = f"本地插件：{display_name}"
    content_label = "本地"
    if load_status == "failed" and last_error:
        info_text = f"加载失败：{last_error}"
        content_label = "加载失败"
    elif load_status == "disabled":
        info_text = f"已禁用本地插件：{display_name}"
        content_label = "已禁用"
    elif upstream_url:
        info_text = upstream_url
    return {
        "id": display_name,
        "name": display_name,
        "description": info_text,
        "version": git.short_commit if git is not None and git.short_commit else "local",
        "author": author,
        "tags": [],
        "icon": "",
        "cover": "",
        "avatar": "",
        "link": upstream_url,
        "branch": branch,
        "type": "danger" if load_status == "failed" else "tip",
        "content": content_label,
        "info": info_text,
        "installMsg": "",
        "alias": [],
        "installed": True,
        "hasUpdate": False,
        "status": "installed",
        "in_store": False,
        "is_git": is_git,
        "local_source_kind": source_kind,
        "local_upstream_url": upstream_url,
        "local_upstream_host": upstream_host,
        "load_status": load_status,
        "last_error": last_error,
        "has_icon": meta.icon_path is not None if meta is not None else False,
    }


@app.get("/api/plugin-store/list", summary="获取插件商店列表", tags=PLUGINS)
async def get_plugin_store_list(
    request: Request,
    _user: SessionRecord = Depends(require_auth),
    tab: Optional[str] = Query(None, description="页签筛选：all / installed / updates / fun / tool / adapter"),
    search: Optional[str] = Query(None, description="按插件名/描述/标签模糊匹配"),
    limit: Optional[int] = Query(None, ge=1, le=200, description="单页条数；不传=全量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    order_seed: Optional[str] = Query(None, min_length=1, max_length=128, description="分页随机排序种子"),
    sort: Optional[str] = Query(None, description="排序方式：default / stars_desc / stars_asc"),
):
    """
    获取远程插件商店的插件列表

    从远程服务器获取可用插件列表，并与本地已安装插件进行比对。
    ``tab == "installed"`` 会合并展示非商店/加载失败本地插件；远端不可达时该页签自动降级。
    """
    import httpx2

    from gsuid_core.utils.plugins_update.api import plugins_lib

    store_offline = False
    plugin_data: dict[str, object] = {}
    try:
        async with httpx2.AsyncClient() as client:
            response = await client.get(plugins_lib, timeout=15)
            response.raise_for_status()
            loaded: object = response.json()
            if isinstance(loaded, dict):
                plugin_data = loaded
    except Exception as e:
        from gsuid_core.logger import logger

        logger.warning(t("log.webconsole.plugin_store_fail", error=e))
        if tab != "installed":
            return {"status": 1, "msg": f"获取插件列表失败: {str(e)}", "data": []}
        store_offline = True

    try:
        raw_plugins_dict = plugin_data["plugins"] if "plugins" in plugin_data else {}
        plugins_dict: dict[str, object] = raw_plugins_dict if isinstance(raw_plugins_dict, dict) else {}

        if plugins_dict:
            plugin_registry.reclassify_with_store(plugins_dict)

        installed_plugins = await get_local_plugins_list()
        installed_ids = set(installed_plugins.keys())

        # 用 store_plugin_id 与 -dev 基名对齐重命名/开发目录，排除单下划线禁用目录防混淆
        local_by_store_id: dict[str, LocalPluginMeta] = {}
        for meta in plugin_registry.list_cached(scope="plugin", include_disabled=False):
            if meta.key not in installed_ids:
                continue
            if meta.git is not None and meta.git.store_plugin_id:
                sid_low = meta.git.store_plugin_id.lower()
                prev_match = local_by_store_id[sid_low] if sid_low in local_by_store_id else None
                if prev_match is None or meta.key == sid_low:
                    local_by_store_id[sid_low] = meta
            if meta.key.endswith("-dev"):
                base_low = meta.key.removesuffix("-dev")
                if base_low not in local_by_store_id:
                    local_by_store_id[base_low] = meta

        formatted_plugins: list[dict[str, object]] = []
        matched_local_keys: set[str] = set()

        for plugin_id, plugin_info_raw in plugins_dict.items():
            plugin_info: dict[str, object] = plugin_info_raw if isinstance(plugin_info_raw, dict) else {}
            pid_lower = plugin_id.lower()
            local_meta: LocalPluginMeta | None = None
            if pid_lower in installed_ids:
                is_installed = True
                matched_local_keys.add(pid_lower)
                local_meta = plugin_registry.get(plugin_id)
            elif pid_lower in local_by_store_id:
                is_installed = True
                local_meta = local_by_store_id[pid_lower]
                matched_local_keys.add(local_meta.key)
            else:
                is_installed = False
            local_git = local_meta.git if local_meta is not None else None

            raw_link = plugin_info["link"] if "link" in plugin_info and isinstance(plugin_info["link"], str) else ""
            raw_info = plugin_info["info"] if "info" in plugin_info and isinstance(plugin_info["info"], str) else ""
            raw_alias = (
                plugin_info["alias"] if "alias" in plugin_info and isinstance(plugin_info["alias"], list) else []
            )
            link_parts = raw_link.split("/") if raw_link else []
            author = link_parts[-2] if len(link_parts) >= 2 else "Unknown"

            formatted_plugins.append(
                {
                    "id": plugin_id,
                    "name": raw_info or plugin_id,
                    "description": raw_info,
                    "version": "latest",
                    "author": author,
                    "tags": raw_alias,
                    "icon": plugin_info["icon"]
                    if "icon" in plugin_info and isinstance(plugin_info["icon"], str)
                    else "",
                    "cover": (
                        plugin_info["cover"] if "cover" in plugin_info and isinstance(plugin_info["cover"], str) else ""
                    ),
                    "avatar": (
                        plugin_info["avatar"]
                        if "avatar" in plugin_info and isinstance(plugin_info["avatar"], str)
                        else ""
                    ),
                    "link": raw_link,
                    "branch": (
                        plugin_info["branch"]
                        if "branch" in plugin_info and isinstance(plugin_info["branch"], str)
                        else "main"
                    ),
                    "type": (
                        plugin_info["type"] if "type" in plugin_info and isinstance(plugin_info["type"], str) else "tip"
                    ),
                    "content": (
                        plugin_info["content"]
                        if "content" in plugin_info and isinstance(plugin_info["content"], str)
                        else "普通"
                    ),
                    "info": raw_info,
                    "installMsg": (
                        plugin_info["installMsg"]
                        if "installMsg" in plugin_info and isinstance(plugin_info["installMsg"], str)
                        else ""
                    ),
                    "alias": raw_alias,
                    "installed": is_installed,
                    "hasUpdate": False,
                    "status": "installed" if is_installed else "not_installed",
                    "in_store": True,
                    "is_git": (bool(local_meta.is_git and local_meta.has_remote) if local_meta is not None else False),
                    "local_plugin_name": local_meta.name if local_meta is not None else None,
                    "local_source_kind": local_meta.source_kind if local_meta is not None else None,
                    "local_upstream_url": local_git.upstream_url if local_git is not None else "",
                    "local_upstream_host": local_git.upstream_host if local_git is not None else "",
                    "load_status": local_meta.load_status if local_meta is not None else None,
                    "last_error": local_meta.last_error if local_meta is not None else None,
                }
            )

        local_only_plugins: list[dict[str, object]] = []
        for local_key, raw_local in installed_plugins.items():
            if local_key in matched_local_keys:
                continue
            local_meta = plugin_registry.get(local_key)
            local_only_plugins.append(_build_local_only_store_entry(local_key, raw_local, local_meta))

        fun_raw = plugin_data["fun_plugins"] if "fun_plugins" in plugin_data else []
        tool_raw = plugin_data["tool_plugins"] if "tool_plugins" in plugin_data else []
        adapter_raw = plugin_data["adapter_plugins"] if "adapter_plugins" in plugin_data else []
        fun_plugins: list[str] = [x for x in fun_raw if isinstance(x, str)] if isinstance(fun_raw, list) else []
        tool_plugins: list[str] = [x for x in tool_raw if isinstance(x, str)] if isinstance(tool_raw, list) else []
        adapter_plugins: list[str] = (
            [x for x in adapter_raw if isinstance(x, str)] if isinstance(adapter_raw, list) else []
        )
        fun_ids = set(fun_plugins)
        tool_ids = set(tool_plugins)
        adapter_ids = set(adapter_plugins)
        plugin_groups = {
            key: value for key, value in plugin_data.items() if key.endswith("_plugins") and isinstance(value, list)
        }

        counts = {
            "all": len(formatted_plugins),
            "installed": sum(1 for p in formatted_plugins if p["installed"]) + len(local_only_plugins),
            "updates": sum(1 for p in formatted_plugins if p["hasUpdate"]),
            "fun": sum(1 for p in formatted_plugins if p["id"] in fun_ids),
            "tool": sum(1 for p in formatted_plugins if p["id"] in tool_ids),
            "adapter": sum(1 for p in formatted_plugins if p["id"] in adapter_ids),
        }

        selected = formatted_plugins
        if tab == "installed":
            selected = [p for p in selected if p["installed"]] + local_only_plugins
        elif tab == "updates":
            selected = [p for p in selected if p["hasUpdate"]]
        elif tab == "fun":
            selected = [p for p in selected if p["id"] in fun_ids]
        elif tab == "tool":
            selected = [p for p in selected if p["id"] in tool_ids]
        elif tab == "adapter":
            selected = [p for p in selected if p["id"] in adapter_ids]

        keyword = (search or "").strip().lower()
        if keyword:
            selected = [
                p
                for p in selected
                if keyword in str(p["id"]).lower()
                or keyword in str(p["description"]).lower()
                or (isinstance(p["tags"], list) and any(keyword in str(tag).lower() for tag in p["tags"]))
            ]

        effective_sort = sort if sort in ("stars_desc", "stars_asc") else "default"
        if effective_sort != "default":
            from gsuid_core.webconsole.github_auth_api import parse_github_repo, resolve_repo_star_counts

            repo_by_id: dict[str, str] = {}
            for p in selected:
                raw_link = p["link"]
                pid = str(p["id"])
                if not p["in_store"] and not raw_link:
                    continue
                link_str = raw_link if isinstance(raw_link, str) and raw_link else f"https://github.com/{pid}"
                repo = parse_github_repo(link_str)
                if repo is not None:
                    repo_by_id[pid] = repo.lower()
            star_counts = await resolve_repo_star_counts(repo_by_id.values())
            descending = effective_sort == "stars_desc"

            def _star_key(p: dict[str, object]) -> tuple[int, int, int, str, str]:
                pid = str(p["id"])
                repo_key = repo_by_id[pid] if pid in repo_by_id else ""
                count = star_counts[repo_key] if repo_key in star_counts else None
                return _plugin_store_star_order_key(
                    pid,
                    deprecated=p["type"] == "danger" and p["content"] == "停止维护",
                    star_count=count,
                    descending=descending,
                    order_seed=order_seed,
                )

            selected.sort(key=_star_key)
        else:
            selected.sort(
                key=lambda p: _plugin_store_order_key(
                    str(p["id"]),
                    deprecated=p["type"] == "danger" and p["content"] == "停止维护",
                    has_update=bool(p["hasUpdate"]),
                    installed_tab=tab == "installed",
                    order_seed=order_seed,
                )
            )

        total_count = len(formatted_plugins)
        count = len(selected)
        page_items = selected[offset:] if limit is None else selected[offset : offset + limit]

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "plugins": page_items,
                "fun_plugins": fun_plugins,
                "tool_plugins": tool_plugins,
                "adapter_plugins": adapter_plugins,
                "plugin_groups": plugin_groups,
                "count": count,
                "total_count": total_count,
                "counts": counts,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(page_items) < count,
                "order_seed": order_seed,
                "sort": effective_sort,
                "store_offline": store_offline,
            },
        }
    except Exception as e:
        from gsuid_core.logger import logger

        logger.warning(t("log.webconsole.plugin_store_fail", error=e))
        return {"status": 1, "msg": f"获取插件列表失败: {str(e)}", "data": []}


@app.post("/api/plugin-store/install/{plugin_id}", summary="通过插件 ID 安装（插件商店白名单）", tags=PLUGINS)
async def install_plugin(
    request: Request, plugin_id: str, repo_url: str = Body(embed=True), _user: Dict[str, Any] = Depends(require_admin)
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

        # install_plugin 内部已完成「安装 + reload_plugin_async 加载」，直接复用其返回信息，
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
    _user: Dict[str, Any] = Depends(require_admin),
):
    """
    从 URL 安装插件（不走插件商店白名单）

    接受前端传来的 git 仓库 URL，后端执行 ``git clone`` 把仓库克隆到
    ``plugins/`` 目录，并通过 ``reload_plugin_async`` 真正加载插件。

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
        from gsuid_core.utils.path_safety import validate_install_source_url
        from gsuid_core.utils.plugins_update._plugins import install_plugin_from_url

        url = (data or {}).get("url")
        branch = (data or {}).get("branch")

        if not isinstance(url, str) or not url.strip():
            return {"status": 1, "msg": "❌ 请提供有效的 git 仓库 URL"}
        url_err = validate_install_source_url(url.strip())
        if url_err:
            return {"status": 1, "msg": f"❌ {url_err}"}

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


@app.post("/api/plugin-store/install-upload", summary="从 zip 文件安装插件", tags=PLUGINS)
async def install_plugin_from_upload_api(
    file: UploadFile = File(..., description="插件 zip 包"),
    _user: Dict[str, Any] = Depends(require_admin),
):
    """
    从上传的 zip 安装插件（不经 git、不走插件商店白名单）。

    插件包必须在 ``__init__.py`` 里声明 ``Plugins(name="唯一名称")``。
    注册名解析不到或为空会直接失败，**不会**回退成 zip 的目录名。

    校验全部在临时目录内完成：任何一步失败，``plugins/`` 下都不会留下残留。
    已安装同名插件时直接拒绝，不做覆盖（覆盖安装留待后续迭代）。

    Args:
        file: 上传的插件 zip
        _user: 认证用户信息

    Returns:
        status: 0 成功，1 失败
        msg: 操作结果（成功以 ✅ 开头，失败以 ❌ 开头）
    """
    from gsuid_core.utils.plugins_update._plugins import install_plugin_from_zip

    try:
        data = await file.read()
        if not data:
            return {"status": 1, "msg": "❌ 上传的文件为空", "data": None}
        result = await install_plugin_from_zip(data)
        if result.lstrip().startswith("❌"):
            return {"status": 1, "msg": result, "data": None}
        return {"status": 0, "msg": "ok", "data": {"msg": result}}
    except Exception as e:
        logger.exception(t("log.webconsole.plugin_install_upload_fail", error=e))
        return {"status": 1, "msg": f"安装失败: {str(e)}", "data": None}


@app.post("/api/plugin-store/update/{plugin_id}", summary="更新已安装插件", tags=PLUGINS)
async def update_plugin(request: Request, plugin_id: str, _user: Dict[str, Any] = Depends(require_admin)):
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
        from gsuid_core.utils.plugins_update._plugins import update_plugins, extract_update_failure

        result = await update_plugins(plugin_id)
        failure_msg = extract_update_failure(result)
        if failure_msg is not None:
            return {"status": 1, "msg": failure_msg}

        return {"status": 0, "msg": "插件更新成功"}
    except Exception as e:
        return {"status": 1, "msg": f"更新失败: {str(e)}"}


@app.delete("/api/plugin-store/uninstall/{plugin_id}", summary="卸载已安装插件", tags=PLUGINS)
async def uninstall_plugin(request: Request, plugin_id: str, _user: SessionRecord = Depends(require_admin)):
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
        from gsuid_core.utils.path_safety import is_safe_filename
        from gsuid_core.utils.plugins_update._plugins import uninstall_plugin

        if not is_safe_filename(plugin_id):
            return {"status": 1, "msg": "非法插件名"}
        found = await resolve_plugin_on_disk(plugin_id, include_core=False, include_buildin=False)
        if found is None:
            # 仅唯一活跃 store_plugin_id 命中时允许按商店 ID 卸载重命名目录（排除禁用目录）
            pid_low = plugin_id.lower()
            candidates = [
                m
                for m in plugin_registry.list_cached(scope="plugin", include_disabled=False)
                if m.git is not None and m.git.store_plugin_id is not None and m.git.store_plugin_id.lower() == pid_low
            ]
            if len(candidates) == 1:
                found = candidates[0]
        if found is None:
            return {"status": 1, "msg": f"❌ 插件 {plugin_id} 不存在!"}
        result = await uninstall_plugin(found.path)

        # 检查结果中是否包含失败标记
        if "❌" in result:
            return {"status": 1, "msg": result}
        else:
            return {"status": 0, "msg": result}
    except Exception as e:
        return {"status": 1, "msg": f"卸载失败: {str(e)}"}
