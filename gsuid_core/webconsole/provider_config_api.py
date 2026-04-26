"""
Provider Config APIs

提供 Provider 配置的 RESTful APIs
统一管理 OpenAI 和 Anthropic 格式的配置，支持高级/低级任务配置切换
"""

from typing import Any, Dict

from fastapi import Depends

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
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


# ==================== Provider 管理 ====================


@app.get("/api/provider_config/providers")
async def get_provider_list(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取支持的 provider 列表

    Returns:
        status: 0成功
        data: provider 列表及各 provider 下的配置统计
    """
    openai_configs = list_available_openai_configs()
    anthropic_configs = list_available_anthropic_configs()

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
                    "configs": openai_configs,
                },
                {
                    "id": "anthropic",
                    "name": "Anthropic 格式",
                    "description": "支持 Claude 系列模型",
                    "config_count": len(anthropic_configs),
                    "configs": anthropic_configs,
                },
            ],
        },
    }


# ==================== 高级/低级任务配置 ====================


@app.get("/api/provider_config/task_config/{task_level}")
async def get_task_config(
    task_level: str,
    _: Dict = Depends(require_auth),
) -> Dict:
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
        config_name = ai_config.get_config(config_key).data

        # 获取可用的配置列表
        available_openai = list_available_openai_configs()
        available_anthropic = list_available_anthropic_configs()

        # 根据 config_name 判断 provider 类型
        if config_name in available_openai:
            provider = "openai"
        elif config_name in available_anthropic:
            provider = "anthropic"
        else:
            provider = None

        # 获取配置的详细信息
        config_detail = None
        if provider:
            manager, config_func = _get_manager_and_config(provider)
            if manager.exists(config_name):
                config = config_func(config_name)
                config_dict = {}
                for key in manager._config_template.keys():
                    cfg = config.get_config(key)
                    config_dict[key] = _string_config_to_dict(cfg)
                config_detail = {
                    "name": config_name,
                    "provider": provider,
                    "config": config_dict,
                }

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "task_level": task_level,
                "current_config": config_name,
                "current_provider": provider,
                "config_detail": config_detail,
                "available_configs": {
                    "openai": available_openai,
                    "anthropic": available_anthropic,
                },
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": str(e),
            "data": None,
        }


@app.post("/api/provider_config/task_config/{task_level}")
async def set_task_config(
    task_level: str,
    data: Dict,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    设置高级或低级任务的配置

    Args:
        task_level: "high" 或 "low"
        data: {"config_name": "...", "provider": "..."}

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

    # 验证 config_name 是否存在
    available_openai = list_available_openai_configs()
    available_anthropic = list_available_anthropic_configs()

    if config_name not in available_openai and config_name not in available_anthropic:
        return {
            "status": 1,
            "msg": f"配置文件 '{config_name}' 不存在",
            "data": None,
        }

    # 根据 config_name 判断 provider
    provider = "openai" if config_name in available_openai else "anthropic"

    try:
        # 设置配置名称
        config_key = f"{task_level}_level_provider_config_name"
        success = ai_config.set_config(config_key, config_name)

        if success:
            return {
                "status": 0,
                "msg": "ok",
                "data": {
                    "task_level": task_level,
                    "config_name": config_name,
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


# ==================== 统一配置管理（不区分 provider） ====================


@app.get("/api/provider_config/all_configs")
async def get_all_configs(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取所有配置（不区分 provider）

    用于前端一次性获取所有配置文件的摘要信息

    Returns:
        status: 0成功
        data: 所有配置列表
    """
    openai_configs = list_available_openai_configs()
    anthropic_configs = list_available_anthropic_configs()

    # 获取每个配置的摘要信息
    openai_summaries = []
    for name in openai_configs:
        try:
            config = get_openai_config(name)
            model_name = config.get_config("model_name").data
            base_url = config.get_config("base_url").data
            openai_summaries.append(
                {
                    "name": name,
                    "provider": "openai",
                    "model_name": model_name,
                    "base_url": base_url,
                }
            )
        except Exception:
            openai_summaries.append(
                {
                    "name": name,
                    "provider": "openai",
                    "model_name": "未知",
                    "base_url": "未知",
                }
            )

    anthropic_summaries = []
    for name in anthropic_configs:
        try:
            config = get_anthropic_config(name)
            model_name = config.get_config("model_name").data
            base_url = config.get_config("base_url").data
            anthropic_summaries.append(
                {
                    "name": name,
                    "provider": "anthropic",
                    "model_name": model_name,
                    "base_url": base_url,
                }
            )
        except Exception:
            anthropic_summaries.append(
                {
                    "name": name,
                    "provider": "anthropic",
                    "model_name": "未知",
                    "base_url": "未知",
                }
            )

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "openai_configs": openai_summaries,
            "anthropic_configs": anthropic_summaries,
            "high_level_config": ai_config.get_config("high_level_provider_config_name").data,
            "low_level_config": ai_config.get_config("low_level_provider_config_name").data,
        },
    }


@app.get("/api/provider_config/config/{provider}/options")
async def get_config_options(
    provider: str,
    _: Dict = Depends(require_auth),
) -> Dict:
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


@app.get("/api/provider_config/config/{provider}/{config_name}")
async def get_config_detail(
    provider: str,
    config_name: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    获取指定配置的详细信息

    Args:
        provider: provider 类型 (openai/anthropic)
        config_name: 配置文件名

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

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "name": config_name,
                "provider": provider,
                "config": config_dict,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": str(e),
            "data": None,
        }


@app.post("/api/provider_config/config/{provider}/{config_name}")
async def create_or_update_config(
    provider: str,
    config_name: str,
    data: Dict,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    创建或更新配置文件

    Args:
        provider: provider 类型 (openai/anthropic)
        config_name: 配置文件名
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

        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "name": config_name,
                "provider": provider,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": str(e),
            "data": None,
        }


@app.post("/api/provider_config/config/{provider}/{config_name}/create_default")
async def create_default_config(
    provider: str,
    config_name: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    创建使用默认配置的新的配置文件

    Args:
        provider: provider 类型 (openai/anthropic)
        config_name: 配置文件名

    Returns:
        status: 0成功，1失败
    """
    if provider not in ["openai", "anthropic"]:
        return {
            "status": 1,
            "msg": f"不支持的 provider 类型: {provider}",
            "data": None,
        }

    if provider == "openai":
        success = create_default_openai_config(config_name)
    else:
        success = create_default_anthropic_config(config_name)

    if success:
        return {
            "status": 0,
            "msg": "ok",
            "data": {"name": config_name, "provider": provider},
        }
    else:
        return {
            "status": 1,
            "msg": f"配置文件 '{config_name}' 已存在",
            "data": None,
        }


@app.delete("/api/provider_config/config/{provider}/{config_name}")
async def delete_config(
    provider: str,
    config_name: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    删除配置文件

    Args:
        provider: provider 类型 (openai/anthropic)
        config_name: 配置文件名

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

    # 检查是否是当前激活的配置
    high_level = ai_config.get_config("high_level_provider_config_name").data
    low_level = ai_config.get_config("low_level_provider_config_name").data

    if high_level == config_name or low_level == config_name:
        return {
            "status": 1,
            "msg": f"无法删除当前激活的配置文件 '{config_name}'，请先切换到其他配置",
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
