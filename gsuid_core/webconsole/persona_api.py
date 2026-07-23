"""
Persona APIs
提供 AI Persona 角色相关的 RESTful APIs
包括列出所有角色、获取角色详情、上传角色资源文件、配置管理等功能
"""

import base64
from typing import Any, Dict

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse

from gsuid_core.ai_core.persona import (
    Persona,
    load_persona,
    save_persona,
    delete_persona,
    get_persona_metadata,
    get_persona_audio_path,
    get_persona_image_path,
    get_persona_avatar_path,
    list_available_personas,
)
from gsuid_core.ai_core.gs_agent import build_new_persona
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth
from gsuid_core.ai_core.persona.config import persona_config_manager
from gsuid_core.ai_core.persona.models import (
    MAX_FILE_SIZE,
    SUPPORTED_AUDIO_FORMATS,
    validate_audio_type,
    validate_image_type,
)

from ._api_tags import PERSONA

# 音频MIME类型映射
AUDIO_MIME_TYPES = {
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}


@app.get("/api/persona/list", summary="获取角色列表", tags=PERSONA)
async def get_persona_list(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取所有可用的 Persona 角色列表

    Returns:
        status: 0成功，1失败
        data: 角色元数据列表，包含名称和文件存在状态
    """
    persona_names = list_available_personas()
    personas_data = []

    for name in persona_names:
        metadata = get_persona_metadata(name)
        personas_data.append(metadata)

    return {
        "status": 0,
        "msg": "ok",
        "data": personas_data,
    }


@app.get("/api/persona/{persona_name}", summary="获取角色详情", tags=PERSONA)
async def get_persona_detail(persona_name: str, _: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取指定 Persona 角色的详细信息

    Args:
        persona_name: 角色名称

    Returns:
        status: 0成功，1失败
        data: 角色详情内容（Markdown格式）和元数据
    """
    try:
        content = await load_persona(persona_name)
        metadata = get_persona_metadata(persona_name)
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "name": persona_name,
                "content": content,
                "metadata": metadata,
            },
        }
    except FileNotFoundError:
        return {
            "status": 1,
            "msg": f"角色 '{persona_name}' 不存在",
            "data": None,
        }


@app.put("/api/persona/{persona_name}/content", summary="更新角色内容", tags=PERSONA)
async def update_persona_content(
    persona_name: str,
    data: Dict[str, Any],
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    更新角色 Markdown 内容

    将新的 Markdown 内容保存到角色的 persona.md 文件中。

    Args:
        persona_name: 角色名称
        data: 包含 content (Markdown字符串) 的请求体

    Returns:
        status: 0成功，1失败
        data: 包含 name 和 content 的对象
    """
    content = data.get("content", "")

    if not content:
        return {
            "status": 1,
            "msg": "请提供内容",
            "data": None,
        }

    # 检查角色是否存在
    persona = Persona(persona_name)
    if not persona.exists():
        return {
            "status": 1,
            "msg": f"角色 '{persona_name}' 不存在",
            "data": None,
        }

    try:
        await save_persona(persona_name, content)
        return {
            "status": 0,
            "msg": "人格内容更新成功",
            "data": {
                "name": persona_name,
                "content": content,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"保存内容失败: {str(e)}",
            "data": None,
        }


@app.get("/api/persona/{persona_name}/avatar", summary="获取角色头像", tags=PERSONA)
async def get_persona_avatar(persona_name: str, _: Dict[str, Any] = Depends(require_auth)) -> FileResponse:
    """
    获取角色头像图片

    Args:
        persona_name: 角色名称

    Returns:
        头像图片文件，如果不存在则返回404
    """
    avatar_path = get_persona_avatar_path(persona_name)
    if not avatar_path:
        raise HTTPException(status_code=404, detail=f"角色 '{persona_name}' 的头像不存在")

    return FileResponse(avatar_path, media_type="image/png")


@app.get("/api/persona/{persona_name}/image", summary="获取角色立绘", tags=PERSONA)
async def get_persona_image(persona_name: str, _: Dict[str, Any] = Depends(require_auth)) -> FileResponse:
    """
    获取角色立绘图片

    Args:
        persona_name: 角色名称

    Returns:
        立绘图片文件，如果不存在则返回404
    """
    image_path = get_persona_image_path(persona_name)
    if not image_path:
        raise HTTPException(status_code=404, detail=f"角色 '{persona_name}' 的立绘不存在")

    return FileResponse(image_path, media_type="image/png")


@app.get("/api/persona/{persona_name}/audio", summary="获取角色音频", tags=PERSONA)
async def get_persona_audio(persona_name: str, _: Dict[str, Any] = Depends(require_auth)) -> FileResponse:
    """
    获取角色音频文件

    Args:
        persona_name: 角色名称

    Returns:
        音频文件，如果不存在则返回404
    """
    audio_path = get_persona_audio_path(persona_name)
    if not audio_path:
        raise HTTPException(status_code=404, detail=f"角色 '{persona_name}' 的音频不存在")

    return FileResponse(audio_path, media_type="audio/mpeg")


@app.post("/api/persona/{persona_name}/avatar", summary="上传角色头像", tags=PERSONA)
async def upload_persona_avatar(
    persona_name: str,
    data: Dict[str, Any],
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    上传角色头像图片

    将 Base64 编码的 PNG 图片保存到 persona 目录下

    Args:
        persona_name: 角色名称
        data: 包含 image (Base64字符串) 的请求体

    Returns:
        status: 0成功，1失败
        data.path: 头像文件路径
    """
    image_base64 = data.get("image", "")
    if not image_base64:
        return {
            "status": 1,
            "msg": "请提供图片数据",
            "data": None,
        }

    # 解析 Base64
    if "," in image_base64:
        header, encoded = image_base64.split(",", 1)
    else:
        encoded = image_base64

    try:
        image_data = base64.b64decode(encoded)
    except Exception:
        return {
            "status": 1,
            "msg": "图片数据无效",
            "data": None,
        }

    # 文件大小检查
    if len(image_data) > MAX_FILE_SIZE:
        return {
            "status": 1,
            "msg": f"图片大小超过限制（最大 {MAX_FILE_SIZE // (1024 * 1024)}MB）",
            "data": None,
        }

    # 图片类型验证（防止上传伪装成图片的可执行文件）
    if not validate_image_type(image_data):
        return {
            "status": 1,
            "msg": "无效的图片格式，仅支持 PNG、JPG、GIF、WebP",
            "data": None,
        }

    # 保存文件
    persona = Persona(persona_name)
    try:
        path = await persona.save_avatar(image_data)
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "path": path,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"保存头像失败: {str(e)}",
            "data": None,
        }


@app.post("/api/persona/{persona_name}/image", summary="上传角色立绘", tags=PERSONA)
async def upload_persona_image(
    persona_name: str,
    data: Dict[str, Any],
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    上传角色立绘图片

    将 Base64 编码的 PNG 图片保存到 persona 目录下

    Args:
        persona_name: 角色名称
        data: 包含 image (Base64字符串) 的请求体

    Returns:
        status: 0成功，1失败
        data.path: 立绘文件路径
    """
    image_base64 = data.get("image", "")
    if not image_base64:
        return {
            "status": 1,
            "msg": "请提供图片数据",
            "data": None,
        }

    # 解析 Base64
    if "," in image_base64:
        header, encoded = image_base64.split(",", 1)
    else:
        encoded = image_base64

    try:
        image_data = base64.b64decode(encoded)
    except Exception:
        return {
            "status": 1,
            "msg": "图片数据无效",
            "data": None,
        }

    # 文件大小检查
    if len(image_data) > MAX_FILE_SIZE:
        return {
            "status": 1,
            "msg": f"图片大小超过限制（最大 {MAX_FILE_SIZE // (1024 * 1024)}MB）",
            "data": None,
        }

    # 图片类型验证
    if not validate_image_type(image_data):
        return {
            "status": 1,
            "msg": "无效的图片格式，仅支持 PNG、JPG、GIF、WebP",
            "data": None,
        }

    # 保存文件
    persona = Persona(persona_name)
    try:
        path = await persona.save_image(image_data)
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "path": path,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"保存立绘失败: {str(e)}",
            "data": None,
        }


@app.post("/api/persona/{persona_name}/audio", summary="上传角色音频", tags=PERSONA)
async def upload_persona_audio(
    persona_name: str,
    data: Dict[str, Any],
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    上传角色音频文件

    将 Base64 编码的音频保存到 persona 目录下
    支持格式：mp3、ogg、wav、m4a、flac（默认mp3）

    Args:
        persona_name: 角色名称
        data: 包含 audio (Base64字符串) 和可选的 format (文件格式) 的请求体

    Returns:
        status: 0成功，1失败
        data.path: 音频文件路径
    """
    audio_base64 = data.get("audio", "")
    if not audio_base64:
        return {
            "status": 1,
            "msg": "请提供音频数据",
            "data": None,
        }

    # 获取文件格式，默认为mp3
    audio_format = data.get("format", "mp3").lower().lstrip(".")
    if audio_format not in [ext.lstrip(".") for ext in SUPPORTED_AUDIO_FORMATS]:
        return {
            "status": 1,
            "msg": f"不支持的音频格式: {audio_format}，支持的格式: mp3, ogg, wav, m4a, flac",
            "data": None,
        }

    # 解析 Base64
    if "," in audio_base64:
        header, encoded = audio_base64.split(",", 1)
    else:
        encoded = audio_base64

    try:
        audio_data = base64.b64decode(encoded)
    except Exception:
        return {
            "status": 1,
            "msg": "音频数据无效",
            "data": None,
        }

    # 文件大小检查
    if len(audio_data) > MAX_FILE_SIZE:
        return {
            "status": 1,
            "msg": f"音频大小超过限制（最大 {MAX_FILE_SIZE // (1024 * 1024)}MB）",
            "data": None,
        }

    # 音频类型验证
    if not validate_audio_type(audio_data, audio_format):
        return {
            "status": 1,
            "msg": f"文件内容与声明的音频格式({audio_format})不匹配",
            "data": None,
        }

    # 保存文件
    persona = Persona(persona_name)
    try:
        path = await persona.save_audio(audio_data, extension=audio_format)
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "path": path,
                "format": audio_format,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"保存音频失败: {str(e)}",
            "data": None,
        }


@app.post("/api/persona/create", summary="创建新角色", tags=PERSONA)
async def create_persona(
    data: Dict[str, Any],
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    创建新角色

    使用 AI 生成角色提示词内容，并保存到 persona 目录。

    Args:
        data: 包含 name (角色名) 和 query (角色描述) 的请求体

    Returns:
        status: 0成功，1失败
        data: 包含 name 和 content 的对象
    """
    name = data.get("name", "").strip()
    query = data.get("query", "").strip()

    if not name:
        return {
            "status": 1,
            "msg": "请提供角色名称",
            "data": None,
        }

    if not query:
        return {
            "status": 1,
            "msg": "请提供角色描述",
            "data": None,
        }

    try:
        content = await build_new_persona(query)
        await save_persona(name, content)
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "name": name,
                "content": content,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"创建角色失败: {str(e)}",
            "data": None,
        }


@app.post("/api/persona/add", summary="直接添加角色", tags=PERSONA)
async def add_persona(
    data: Dict[str, Any],
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    直接添加新角色（简单模式）

    前端只需输入角色名称和内容，即可直接保存人格。
    适用于用户自己编写人格内容的场景。

    Args:
        data: 包含 name (角色名) 和 content (角色内容) 的请求体

    Returns:
        status: 0成功，1失败
        data: 包含 name 和 content 的对象
    """
    name = data.get("name", "").strip()
    content = data.get("content", "").strip()

    if not name:
        return {
            "status": 1,
            "msg": "请提供角色名称",
            "data": None,
        }

    if not content:
        return {
            "status": 1,
            "msg": "请提供角色内容",
            "data": None,
        }

    # 检查角色是否已存在
    persona = Persona(name)
    if persona.exists():
        return {
            "status": 1,
            "msg": f"角色 '{name}' 已存在",
            "data": None,
        }

    try:
        await save_persona(name, content)
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "name": name,
                "content": content,
            },
        }
    except Exception as e:
        return {
            "status": 1,
            "msg": f"添加角色失败: {str(e)}",
            "data": None,
        }


@app.delete("/api/persona/{persona_name}", summary="删除角色", tags=PERSONA)
async def remove_persona(
    persona_name: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    删除角色

    删除 persona 目录下该角色的整个文件夹。

    Args:
        persona_name: 角色名称

    Returns:
        status: 0成功，1失败
    """
    deleted = delete_persona(persona_name)
    if deleted:
        return {
            "status": 0,
            "msg": "ok",
            "data": None,
        }
    else:
        return {
            "status": 1,
            "msg": f"角色 '{persona_name}' 不存在",
            "data": None,
        }


# ==================== Persona 配置管理 API ====================


@app.get("/api/persona/{persona_name}/config", summary="获取角色配置", tags=PERSONA)
async def get_persona_config(
    persona_name: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    获取指定 Persona 的配置

    Args:
        persona_name: 角色名称

    Returns:
        status: 0成功，1失败
        data: 配置对象，包含 ai_mode, scope, target_groups
    """
    config = persona_config_manager.get_persona_config_dict(persona_name)
    if config is None:
        return {
            "status": 1,
            "msg": f"角色 '{persona_name}' 的配置不存在",
            "data": None,
        }

    return {
        "status": 0,
        "msg": "ok",
        "data": config,
    }


@app.put("/api/persona/{persona_name}/config", summary="更新角色配置", tags=PERSONA)
async def update_persona_config(
    persona_name: str,
    data: Dict[str, Any],
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """
    更新指定 Persona 的配置

    Args:
        persona_name: 角色名称
        data: 配置对象，可包含 ai_mode, scope, target_groups

    Returns:
        status: 0成功，1失败
        data: 更新后的配置对象

    注意：
    - scope 可选值为: "disabled"(不对任何群聊启用), "global"(对所有群/角色启用), "specific"(仅对指定群聊启用)
    - 全部人格中只能有一个配置为 "global"
    """
    # 检查 persona 是否存在
    persona = Persona(persona_name)
    if not persona.exists():
        return {
            "status": 1,
            "msg": f"角色 '{persona_name}' 不存在",
            "data": None,
        }

    results = []

    # 更新 scope（如果提供）
    if "scope" in data:
        scope = data["scope"]
        success, msg = persona_config_manager.set_scope(persona_name, scope)
        if not success:
            return {
                "status": 1,
                "msg": msg,
                "data": None,
            }
        results.append(f"scope: {scope}")

    # 更新 target_groups（如果提供）
    if "target_groups" in data:
        target_groups = data["target_groups"]
        if not isinstance(target_groups, list):
            return {
                "status": 1,
                "msg": "target_groups 必须是列表",
                "data": None,
            }
        success, msg = persona_config_manager.set_target_groups(persona_name, target_groups)
        if not success:
            return {
                "status": 1,
                "msg": msg,
                "data": None,
            }
        results.append(f"target_groups: {target_groups}")

    # 更新 ai_mode（如果提供）
    if "ai_mode" in data:
        ai_mode = data["ai_mode"]
        if not isinstance(ai_mode, list):
            return {
                "status": 1,
                "msg": "ai_mode 必须是列表",
                "data": None,
            }
        success, msg = persona_config_manager.set_ai_mode(persona_name, ai_mode)
        if not success:
            return {
                "status": 1,
                "msg": msg,
                "data": None,
            }
        results.append(f"ai_mode: {ai_mode}")

        # 如果启用了定时巡检，启动该 persona 的巡检任务
        if "定时巡检" in ai_mode:
            from gsuid_core.ai_core.heartbeat import start_heartbeat_inspector

            start_heartbeat_inspector()

    # 更新 inspect_interval（如果提供）
    if "inspect_interval" in data:
        inspect_interval = data["inspect_interval"]
        if not isinstance(inspect_interval, int):
            return {
                "status": 1,
                "msg": "inspect_interval 必须是整数",
                "data": None,
            }
        success, msg = persona_config_manager.set_inspect_interval(persona_name, inspect_interval)
        if not success:
            return {
                "status": 1,
                "msg": msg,
                "data": None,
            }
        results.append(f"inspect_interval: {inspect_interval}")

        # 如果该 persona 已启用定时巡检，重新启动以应用新间隔
        config = persona_config_manager.get_config(persona_name)
        if "定时巡检" in config.get_config("ai_mode").data:
            from gsuid_core.ai_core.heartbeat.inspector import get_inspector

            inspector = get_inspector()
            inspector.stop_for_persona(persona_name)
            inspector.start_for_persona(persona_name)

    # 更新 keywords（如果提供）
    if "keywords" in data:
        keywords = data["keywords"]
        if not isinstance(keywords, list):
            return {
                "status": 1,
                "msg": "keywords 必须是列表",
                "data": None,
            }
        success, msg = persona_config_manager.set_keywords(persona_name, keywords)
        if not success:
            return {
                "status": 1,
                "msg": msg,
                "data": None,
            }
        results.append(f"keywords: {keywords}")

    # 更新 tool_packs（如果提供）
    if "tool_packs" in data:
        tool_packs = data["tool_packs"]
        if not isinstance(tool_packs, list):
            return {
                "status": 1,
                "msg": "tool_packs 必须是列表",
                "data": None,
            }
        success, msg = persona_config_manager.set_tool_packs(persona_name, [str(x) for x in tool_packs])
        if not success:
            return {
                "status": 1,
                "msg": msg,
                "data": None,
            }
        results.append(f"tool_packs: {tool_packs}")

    # 更新 tool_names（如果提供）
    if "tool_names" in data:
        tool_names = data["tool_names"]
        if not isinstance(tool_names, list):
            return {
                "status": 1,
                "msg": "tool_names 必须是列表",
                "data": None,
            }
        success, msg = persona_config_manager.set_tool_names(persona_name, [str(x) for x in tool_names])
        if not success:
            return {
                "status": 1,
                "msg": msg,
                "data": None,
            }
        results.append(f"tool_names: {tool_names}")

    # 返回更新后的配置
    updated_config = persona_config_manager.get_persona_config_dict(persona_name)
    return {
        "status": 0,
        "msg": f"已更新: {', '.join(results)}" if results else "没有更新任何配置",
        "data": updated_config,
    }


@app.get("/api/persona/config/global", summary="获取全局启用的角色", tags=PERSONA)
async def get_global_persona(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取当前配置为全局启用的 Persona

    Returns:
        status: 0成功，1失败
        data: 全局启用的 Persona 名称，如果没有则返回 null
    """
    global_persona = persona_config_manager.get_global_persona()
    return {
        "status": 0,
        "msg": "ok",
        "data": global_persona,
    }


@app.get("/api/persona/config/all", summary="获取所有角色配置", tags=PERSONA)
async def get_all_persona_configs(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """
    获取所有 Persona 的配置

    Returns:
        status: 0成功
        data: 字典，key 为 persona 名称，value 为配置对象
    """
    configs = {}
    for persona_name in list_available_personas():
        config = persona_config_manager.get_persona_config_dict(persona_name)
        if config is not None:
            configs[persona_name] = config

    return {
        "status": 0,
        "msg": "ok",
        "data": configs,
    }


@app.get("/api/persona/heartbeat/status", summary="Heartbeat 巡检运行态", tags=PERSONA)
async def get_heartbeat_status(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """返回每个 Persona 的 Heartbeat 巡检 job 是否已注册，以及配置侧是否启用定时巡检。"""
    try:
        from gsuid_core.ai_core.heartbeat.inspector import get_inspector

        inspector = get_inspector()
        scheduled = set(getattr(inspector, "_scheduled_jobs", {}) or {})
        items = []
        for persona_name in list_available_personas():
            cfg = persona_config_manager.get_persona_config_dict(persona_name) or {}
            ai_mode = cfg.get("ai_mode") or []
            if isinstance(ai_mode, dict):
                ai_mode = ai_mode.get("value") or ai_mode.get("data") or []
            inspect_interval = cfg.get("inspect_interval")
            if isinstance(inspect_interval, dict):
                inspect_interval = inspect_interval.get("value") or inspect_interval.get("data")
            enabled = "定时巡检" in (ai_mode if isinstance(ai_mode, list) else [])
            job_id = (getattr(inspector, "_scheduled_jobs", {}) or {}).get(persona_name)
            items.append(
                {
                    "persona_name": persona_name,
                    "enabled": enabled,
                    "inspect_interval": inspect_interval,
                    "job_registered": persona_name in scheduled,
                    "job_id": job_id,
                }
            )
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "inspector_running": bool(getattr(inspector, "is_running", False)),
                "items": items,
                "count": len(items),
            },
        }
    except Exception as e:
        return {"status": 1, "msg": f"获取 Heartbeat 状态失败: {e}", "data": None}
