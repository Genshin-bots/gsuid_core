"""
Provider Config APIs

提供 Provider 配置的 RESTful APIs
统一管理 OpenAI 和 Anthropic 格式的配置，支持高级/低级任务配置切换

配置名称格式: "provider++config_name" (例如 "openai++MiniMAX")
- provider: "openai" 或 "anthropic"
- config_name: 配置文件名称
- 分隔符: "++"
- 兼容旧格式: 不含 "++" 的名称默认按 "openai" provider 处理
"""

from typing import Any, Dict

from fastapi import Depends

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.configs.models import (
    parse_provider_config_name,
    format_provider_config_name,
)
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.configs.openai_config import (
    get_openai_config,
    openai_config_manager as openai_manager,
    create_default_openai_config,
    list_available_openai_configs,
)
from gsuid_core.ai_core.configs.anthropic_config import (
    get_anthropic_config,
    anthropic_config_manager as anthropic_manager,
    create_default_anthropic_config,
    list_available_anthropic_configs,
)

from ._api_tags import PROVIDER_CONFIG


def _string_config_to_dict(config: Any) -> Dict[str, Any]:
    """将 StringConfig 对象转换为字典用于 JSON 序列化"""
    return {
        "title": config.title,
        "desc": config.desc,
        "data": config.data,
        "options": getattr(config, "options", []),
    }


def _get_manager_and_config(provider: str) -> tuple[Any, Any]:
    """根据 provider 类型获取对应的 manager 和 config 获取函数"""
    if provider == "openai":
        return openai_manager, get_openai_config
    else:
        return anthropic_manager, get_anthropic_config


def _validate_config_name_no_plus(config_name: str) -> Dict[str, Any] | None:
    """
    验证配置名称不包含 "+" 号。

    Args:
        config_name: 要验证的配置名称

    Returns:
        None 表示验证通过，否则返回错误响应字典
    """
    if "+" in config_name:
        return {
            "status": 1,
            "msg": (
                f"配置文件名称 '{config_name}' 包含非法字符 '+'。"
                f"配置文件名称不允许包含 '+' 字符，"
                f"因为 '+' 是 provider 与配置名称的分隔符（格式: provider++config_name）。"
                f"请使用不含 '+' 的名称重新创建。"
            ),
            "data": None,
        }
    return None


def _build_all_configs_summary() -> Dict[str, Any]:
    """
    构建所有配置的摘要信息，返回 provider++name 格式的名称。

    Returns:
        包含所有配置摘要的字典
    """
    openai_configs = list_available_openai_configs()
    anthropic_configs = list_available_anthropic_configs()

    all_summaries: list[Dict[str, Any]] = []

    for name in openai_configs:
        full_name = format_provider_config_name("openai", name)
        try:
            config = get_openai_config(name)
            model_name = config.get_config("model_name").data
            base_url = config.get_config("base_url").data
            all_summaries.append(
                {
                    "name": full_name,
                    "provider": "openai",
                    "config_name": name,
                    "model_name": model_name,
                    "base_url": base_url,
                }
            )
        except Exception:
            all_summaries.append(
                {
                    "name": full_name,
                    "provider": "openai",
                    "config_name": name,
                    "model_name": "未知",
                    "base_url": "未知",
                }
            )

    for name in anthropic_configs:
        full_name = format_provider_config_name("anthropic", name)
        try:
            config = get_anthropic_config(name)
            model_name = config.get_config("model_name").data
            base_url = config.get_config("base_url").data
            all_summaries.append(
                {
                    "name": full_name,
                    "provider": "anthropic",
                    "config_name": name,
                    "model_name": model_name,
                    "base_url": base_url,
                }
            )
        except Exception:
            all_summaries.append(
                {
                    "name": full_name,
                    "provider": "anthropic",
                    "config_name": name,
                    "model_name": "未知",
                    "base_url": "未知",
                }
            )

    return {
        "configs": all_summaries,
        "high_level_config": ai_config.get_config("high_level_provider_config_name").data,
        "low_level_config": ai_config.get_config("low_level_provider_config_name").data,
    }


# ==================== Provider 管理 ====================


@app.get("/api/provider_config/providers", summary="获取 Provider 列表", tags=PROVIDER_CONFIG)
async def get_provider_list(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取支持的 provider 列表

    Returns:
        status: 0成功
        data: provider 列表及各 provider 下的配置统计
    """
    openai_configs = list_available_openai_configs()
    anthropic_configs = list_available_anthropic_configs()

    # 返回 provider++name 格式的配置名称列表
    openai_full_names = [format_provider_config_name("openai", name) for name in openai_configs]
    anthropic_full_names = [format_provider_config_name("anthropic", name) for name in anthropic_configs]

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "providers": [
                {
                    "id": "openai",
                    "name": "OpenAI 兼容格式",
                    "description": "支持 OpenAI、Azure、第三方兼容 API",
                    "config_count": len(openai_configs),
                    "configs": openai_full_names,
                },
                {
                    "id": "anthropic",
                    "name": "Anthropic 格式",
                    "description": "支持 Claude 系列模型",
                    "config_count": len(anthropic_configs),
                    "configs": anthropic_full_names,
                },
            ],
        },
    }


# ==================== 高级/低级任务配置 ====================


@app.get("/api/provider_config/task_config/{task_level}", summary="获取任务级别配置", tags=PROVIDER_CONFIG)
async def get_task_config(
    task_level: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取高级或低级任务的配置详情

    Args:
        task_level: "high" 或 "low"

    Returns:
        status: 0成功，1失败
        data: 配置详情
    """
    if task_level not in ["high", "low"]:
        return {
            "status": 1,
            "msg": "task_level 必须是 'high' 或 'low'",
            "data": None,
        }

    try:
        config_key = f"{task_level}_level_provider_config_name"
        full_config_name = ai_config.get_config(config_key).data

        # 解析 provider++name 格式
        provider, config_name = parse_provider_config_name(full_config_name)

        # 获取配置的详细信息
        config_detail = None
        manager, config_func = _get_manager_and_config(provider)
        if manager.exists(config_name):
            config = config_func(config_name)
            config_dict = {}
            for key in manager._config_template.keys():
                cfg = config.get_config(key)
                config_dict[key] = _string_config_to_dict(cfg)
            config_detail = {
                "name": full_config_name,
                "provider": provider,
                "config_name": config_name,
                "config": config_dict,
            }

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "task_level": task_level,
                "current_config": full_config_name,
                "current_provider": provider,
                "config_detail": config_detail,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": str(e),
            "data": None,
        }


@app.post("/api/provider_config/task_config/{task_level}", summary="设置任务级别配置", tags=PROVIDER_CONFIG)
async def set_task_config(
    task_level: str,
    data: Dict[str, Any],
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    设置高级或低级任务的配置

    Args:
        task_level: "high" 或 "low"
        data: {"config_name": "provider++config_name"}
              例如 {"config_name": "openai++MiniMAX"}
              兼容旧格式: {"config_name": "MiniMAX"} 默认按 openai 处理

    Returns:
        status: 0成功，1失败
    """
    if task_level not in ["high", "low"]:
        return {
            "status": 1,
            "msg": "task_level 必须是 'high' 或 'low'",
            "data": None,
        }

    config_name = data.get("config_name")

    if not config_name:
        return {
            "status": 1,
            "msg": "缺少 config_name 参数",
            "data": None,
        }

    # 解析 provider++name 格式（兼容旧格式）
    try:
        provider, actual_config_name = parse_provider_config_name(config_name)
    except ValueError as e:
        return {
            "status": 1,
            "msg": str(e),
            "data": None,
        }

    # 验证配置文件是否存在
    manager, _ = _get_manager_and_config(provider)
    if not manager.exists(actual_config_name):
        return {
            "status": 1,
            "msg": f"配置文件 '{actual_config_name}' 在 {provider} provider 中不存在",
            "data": None,
        }

    try:
        # 存储 provider++name 格式的完整名称
        full_name = format_provider_config_name(provider, actual_config_name)
        config_key = f"{task_level}_level_provider_config_name"
        success = ai_config.set_config(config_key, full_name)

        if success:
            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "task_level": task_level,
                    "config_name": full_name,
                    "provider": provider,
                },
            }
        else:
            return {
                "status": 1,
                "msg": "配置设置失败",
                "data": None,
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": str(e),
            "data": None,
        }


@app.delete("/api/provider_config/task_config/{task_level}", summary="清除任务级别配置", tags=PROVIDER_CONFIG)
async def clear_task_config(
    task_level: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    清除高级或低级任务的配置（将配置名置空）

    Args:
        task_level: "high" 或 "low"

    Returns:
        status: 0成功，1失败
        data: 操作结果
    """
    if task_level not in ["high", "low"]:
        return {
            "status": 1,
            "msg": "task_level 必须是 'high' 或 'low'",
            "data": None,
        }

    try:
        config_key = f"{task_level}_level_provider_config_name"
        success = ai_config.set_config(config_key, "")

        if success:
            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "task_level": task_level,
                    "config_name": "",
                },
            }
        else:
            return {
                "status": 1,
                "msg": "清除任务配置失败",
                "data": None,
            }
    except Exception as e:
        return {
            "status": 1,
            "msg": str(e),
            "data": None,
        }


# ==================== 统一配置管理（不区分 provider） ====================


@app.get("/api/provider_config/all_configs", summary="获取所有配置摘要", tags=PROVIDER_CONFIG)
async def get_all_configs(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取所有配置（不区分 provider）

    用于前端一次性获取所有配置文件的摘要信息
    所有配置名称均使用 "provider++config_name" 格式

    Returns:
        status: 0成功
        data: 所有配置列表
    """
    result = _build_all_configs_summary()

    return {
        "status": 0,
        "msg": "ok",
        "data": result,
    }


@app.get("/api/provider_config/config/{provider}/options", summary="获取配置可选项", tags=PROVIDER_CONFIG)
async def get_config_options(
    provider: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取指定 provider 的配置可选项

    Args:
        provider: provider 类型 (openai/anthropic)

    Returns:
        status: 0成功
        data: 各配置项的可选项
    """
    if provider not in ["openai", "anthropic"]:
        return {
            "status": 1,
            "msg": f"不支持的 provider 类型: {provider}",
            "data": None,
        }

    manager, _ = _get_manager_and_config(provider)

    # 返回模板中的 options
    options = {}
    for key, config_template in manager._config_template.items():
        opts = getattr(config_template, "options", None)
        if opts is not None:
            options[key] = opts
        else:
            options[key] = []

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "provider": provider,
            "options": options,
        },
    }


@app.get("/api/provider_config/config/{provider}/{config_name}", summary="获取配置详情", tags=PROVIDER_CONFIG)
async def get_config_detail(
    provider: str,
    config_name: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取指定配置的详细信息

    Args:
        provider: provider 类型 (openai/anthropic)
        config_name: 配置文件名（不含 provider 前缀）

    Returns:
        status: 0成功，1失败
        data: 配置详情
    """
    if provider not in ["openai", "anthropic"]:
        return {
            "status": 1,
            "msg": f"不支持的 provider 类型: {provider}",
            "data": None,
        }

    try:
        manager, config_func = _get_manager_and_config(provider)

        if not manager.exists(config_name):
            return {
                "status": 1,
                "msg": f"配置文件 '{config_name}' 不存在",
                "data": None,
            }

        config = config_func(config_name)
        config_dict = {}
        for key in manager._config_template.keys():
            cfg = config.get_config(key)
            config_dict[key] = _string_config_to_dict(cfg)

        full_name = format_provider_config_name(provider, config_name)

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "name": full_name,
                "provider": provider,
                "config_name": config_name,
                "config": config_dict,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": str(e),
            "data": None,
        }


@app.post("/api/provider_config/config/{provider}/{config_name}", summary="创建或更新配置", tags=PROVIDER_CONFIG)
async def create_or_update_config(
    provider: str,
    config_name: str,
    data: Dict[str, Any],
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    创建或更新配置文件

    Args:
        provider: provider 类型 (openai/anthropic)
        config_name: 配置文件名（不含 provider 前缀，不允许包含 '+' 字符）
        data: 配置数据 {"config": {...}}

    Returns:
        status: 0成功，1失败
    """
    if provider not in ["openai", "anthropic"]:
        return {
            "status": 1,
            "msg": f"不支持的 provider 类型: {provider}",
            "data": None,
        }

    # 拒绝配置名称包含 + 号的请求
    plus_error = _validate_config_name_no_plus(config_name)
    if plus_error is not None:
        return plus_error

    config_data = data.get("config", {})

    try:
        manager, config_func = _get_manager_and_config(provider)

        # 获取或创建配置
        config = config_func(config_name)

        # 只更新模板中存在的字段
        valid_keys = set(manager._config_template.keys())
        for key, value in config_data.items():
            if key not in valid_keys:
                # 跳过不存在的字段
                continue
            if isinstance(value, dict) and "data" in value:
                success = config.set_config(key, value["data"])
            else:
                success = config.set_config(key, value)

            if not success:
                return {
                    "status": 1,
                    "msg": f"设置配置项 '{key}' 失败",
                    "data": None,
                }

        full_name = format_provider_config_name(provider, config_name)

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "name": full_name,
                "provider": provider,
                "config_name": config_name,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": str(e),
            "data": None,
        }


@app.post(
    "/api/provider_config/config/{provider}/{config_name}/create_default",
    summary="创建默认配置",
    tags=PROVIDER_CONFIG,
)
async def create_default_config(
    provider: str,
    config_name: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    创建使用默认配置的新的配置文件

    Args:
        provider: provider 类型 (openai/anthropic)
        config_name: 配置文件名（不允许包含 '+' 字符）

    Returns:
        status: 0成功，1失败
    """
    if provider not in ["openai", "anthropic"]:
        return {
            "status": 1,
            "msg": f"不支持的 provider 类型: {provider}",
            "data": None,
        }

    # 拒绝配置名称包含 + 号的请求
    plus_error = _validate_config_name_no_plus(config_name)
    if plus_error is not None:
        return plus_error

    if provider == "openai":
        success = create_default_openai_config(config_name)
    else:
        success = create_default_anthropic_config(config_name)

    if success:
        full_name = format_provider_config_name(provider, config_name)
        return {
            "status": 0,
            "msg": "ok",
            "data": {"name": full_name, "provider": provider, "config_name": config_name},
        }
    else:
        return {
            "status": 1,
            "msg": f"配置文件 '{config_name}' 已存在",
            "data": None,
        }


@app.delete("/api/provider_config/config/{provider}/{config_name}", summary="删除配置", tags=PROVIDER_CONFIG)
async def delete_config(
    provider: str,
    config_name: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    删除配置文件

    Args:
        provider: provider 类型 (openai/anthropic)
        config_name: 配置文件名（不含 provider 前缀）

    Returns:
        status: 0成功，1失败
    """
    if provider not in ["openai", "anthropic"]:
        return {
            "status": 1,
            "msg": f"不支持的 provider 类型: {provider}",
            "data": None,
        }

    manager, _ = _get_manager_and_config(provider)

    # 检查是否是当前激活的配置（使用 provider++name 格式比较）
    high_level = ai_config.get_config("high_level_provider_config_name").data
    low_level = ai_config.get_config("low_level_provider_config_name").data

    full_name = format_provider_config_name(provider, config_name)

    if high_level == full_name or low_level == full_name:
        return {
            "status": 1,
            "msg": f"无法删除当前激活的配置文件 '{full_name}'，请先切换到其他配置",
            "data": None,
        }

    deleted = manager.delete(config_name)
    if deleted:
        return {
            "status": 0,
            "msg": "ok",
            "data": None,
        }
    else:
        return {
            "status": 1,
            "msg": f"配置文件 '{config_name}' 不存在",
            "data": None,
        }
