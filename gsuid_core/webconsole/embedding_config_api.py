"""
Embedding Config APIs

提供嵌入模型配置的 RESTful APIs
支持查看和修改嵌入模型提供方（local/openai/插件注册的第三方）及其配置
"""

from typing import Any, Dict

from fastapi import Depends

from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.configs.ai_config import (
    ai_config,
    local_embedding_config,
    openai_embedding_config,
)
from gsuid_core.ai_core.rag.embedding_registry import (
    list_external_providers,
    list_embedding_providers,
)


def _string_config_to_dict(config: Any) -> Dict[str, Any]:
    """将 StringConfig 对象转换为字典用于 JSON 序列化"""
    return {
        "title": config.title,
        "desc": config.desc,
        "data": config.data,
        "options": getattr(config, "options", []),
    }


def _build_extra_providers() -> Dict[str, Any]:
    """构建插件注册 provider 的摘要信息（与 local/openai 配置格式同构）"""
    extra: Dict[str, Any] = {}
    for name, entry in list_external_providers().items():
        config_dict: Dict[str, Any] = {}
        source = entry.config_source
        if source is not None:
            for key in source:  # StringConfig 可迭代其配置键
                config_dict[key] = _string_config_to_dict(source.get_config(key))
        extra[name] = {
            "display_name": entry.display_name or name,
            "plugin": entry.plugin,
            "kind": entry.kind,
            "config": config_dict,
        }
    return extra


# ==================== 嵌入模型配置 ====================


@app.get("/api/embedding_config/provider")
async def get_embedding_provider(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取当前嵌入模型提供方

    Returns:
        status: 0成功
        data: 当前嵌入模型提供方信息
    """
    provider = ai_config.get_config("embedding_provider").data
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "provider": provider,
            "available_providers": list_embedding_providers(),
        },
    }


@app.post("/api/embedding_config/provider")
async def set_embedding_provider(data: Dict, _: Dict = Depends(require_auth)) -> Dict:
    """
    设置嵌入模型提供方

    Args:
        data: {"provider": "local" | "openai" | 插件注册的 provider 名}

    Returns:
        status: 0成功
    """
    provider = data.get("provider", "")
    available = list_embedding_providers()
    if provider not in available:
        return {
            "status": 1,
            "msg": f"不支持的嵌入模型提供方: '{provider}'，可用: {available}",
            "data": None,
        }

    ai_config.set_config("embedding_provider", provider)

    # 重置嵌入提供方单例，下次使用时会重新初始化
    from gsuid_core.ai_core.rag.embedding import reset_embedding_provider

    reset_embedding_provider()

    return {
        "status": 0,
        "msg": f"嵌入模型提供方已切换为 '{provider}'，重启后生效",
        "data": {"provider": provider},
    }


@app.get("/api/embedding_config/local")
async def get_local_embedding_config(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取本地嵌入模型配置

    Returns:
        status: 0成功
        data: 本地嵌入模型配置详情
    """
    config_dict = {}
    for key in local_embedding_config.config:
        config_dict[key] = _string_config_to_dict(local_embedding_config.get_config(key))

    return {
        "status": 0,
        "msg": "ok",
        "data": config_dict,
    }


@app.post("/api/embedding_config/local")
async def set_local_embedding_config(data: Dict, _: Dict = Depends(require_auth)) -> Dict:
    """
    保存本地嵌入模型配置

    Args:
        data: 配置项键值对，如 {"embedding_model_name": "BAAI/bge-small-zh-v1.5"}

    Returns:
        status: 0成功
    """
    for key, value in data.items():
        local_embedding_config.set_config(key, value)

    return {
        "status": 0,
        "msg": "本地嵌入模型配置已保存，重启后生效",
        "data": None,
    }


@app.get("/api/embedding_config/openai")
async def get_openai_embedding_config(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取 OpenAI 嵌入模型配置

    Returns:
        status: 0成功
        data: OpenAI 嵌入模型配置详情
    """
    config_dict = {}
    for key in openai_embedding_config.config:
        config_dict[key] = _string_config_to_dict(openai_embedding_config.get_config(key))

    return {
        "status": 0,
        "msg": "ok",
        "data": config_dict,
    }


@app.post("/api/embedding_config/openai")
async def set_openai_embedding_config(data: Dict, _: Dict = Depends(require_auth)) -> Dict:
    """
    保存 OpenAI 嵌入模型配置

    Args:
        data: 配置项键值对，如 {"base_url": "...", "api_key": ["sk-xxx"], "embedding_model": "..."}

    Returns:
        status: 0成功
    """
    for key, value in data.items():
        openai_embedding_config.set_config(key, value)

    return {
        "status": 0,
        "msg": "OpenAI 嵌入模型配置已保存，重启后生效",
        "data": None,
    }


@app.get("/api/embedding_config/summary")
async def get_embedding_config_summary(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取嵌入模型配置摘要（一次性获取所有信息）

    Returns:
        status: 0成功
        data: 嵌入模型配置摘要
    """
    provider = ai_config.get_config("embedding_provider").data

    # 本地配置
    local_config = {}
    for key in local_embedding_config.config:
        local_config[key] = _string_config_to_dict(local_embedding_config.get_config(key))

    # OpenAI 配置
    openai_config_dict = {}
    for key in openai_embedding_config.config:
        openai_config_dict[key] = _string_config_to_dict(openai_embedding_config.get_config(key))

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "provider": provider,
            "available_providers": list_embedding_providers(),
            "local_config": local_config,
            "openai_config": openai_config_dict,
            # 插件注册的 provider（前端未跟进时静默忽略，向后兼容）
            "extra_providers": _build_extra_providers(),
        },
    }
