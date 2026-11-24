"""
Persona APIs
提供 AI Persona 角色相关的 RESTful APIs
包括列出所有角色、获取角色详情、上传角色资源文件、配置管理等功能
"""

import base64
from typing import Any, Dict, List, TypedDict
from dataclasses import field, dataclass

from fastapi import Body, Depends, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

import gsuid_core.webconsole.persona_store_api  # noqa: F401
import gsuid_core.webconsole.persona_bundle_api  # noqa: F401
from gsuid_core.ai_core.persona import (
    Persona,
    copy_persona,
    load_persona,
    save_persona,
    get_persona_metadata,
    get_persona_audio_path,
    get_persona_image_path,
    get_persona_avatar_path,
    list_available_personas,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth, require_admin
from gsuid_core.ai_core.persona.config import persona_config_manager
from gsuid_core.ai_core.persona.models import (
    MAX_FILE_SIZE,
    SUPPORTED_AUDIO_FORMATS,
    validate_audio_type,
    validate_image_type,
)
from gsuid_core.webconsole.plugins_api import _build_config_item
from gsuid_core.ai_core.persona.settings import (
    DEFAULT_PERSONA_SETTINGS,
    persona_settings_manager,
)
from gsuid_core.webconsole.session_store import SessionRecord
from gsuid_core.utils.plugins_config.models import GsDivider

from ._api_tags import PERSONA


def _is_builtin_persona(name: str) -> bool:
    """程序自带的默认人格。启动会用种子覆盖它的正文。"""
    from gsuid_core.ai_core.prompt_group.catalog import BUILTIN_PERSONA_NAME

    return name == BUILTIN_PERSONA_NAME


def _managed_block(persona_name: str) -> Dict[str, Any] | None:
    """git 人格内容由仓库拥有，只许改启用范围；其余写操作在这里拦下。"""
    from gsuid_core.ai_core.persona.store import is_git_managed

    if is_git_managed(persona_name):
        return {
            "status": 1,
            "msg": "该人格由人格市场管理，不允许直接修改；可复制一份后编辑副本。",
            "data": None,
        }
    return None


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


@app.get(
    "/api/persona/config/placeholder-preview",
    summary="已激活人格的标签填入值",
    tags=PERSONA,
)
async def get_placeholder_preview(_: Dict[str, Any] = Depends(require_auth)) -> Dict[str, Any]:
    """提示词组页只读展示。开关打开、且人格目录还在的才列出。

    不放在 /api/persona/{name}：那个路由先注册，两段路径会被它接走。
    """
    from gsuid_core.ai_core.persona.placeholder_preview import active_placeholder_preview

    return {"status": 0, "msg": "ok", "data": active_placeholder_preview()}


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
    except ValueError:
        return {
            "status": 1,
            "msg": "非法角色名",
            "data": None,
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
    blocked = _managed_block(persona_name)
    if blocked is not None:
        return blocked
    content = data.get("content", "")

    if not content:
        return {
            "status": 1,
            "msg": "请提供内容",
            "data": None,
        }

    if _is_builtin_persona(persona_name):
        return {
            "status": 1,
            "msg": "默认人格「早柚」的人格卡每次启动从程序写回，不能在这里修改。请先复制一份。",
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
    try:
        avatar_path = get_persona_avatar_path(persona_name)
    except ValueError:
        raise HTTPException(status_code=404, detail="非法角色名")
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
    try:
        image_path = get_persona_image_path(persona_name)
    except ValueError:
        raise HTTPException(status_code=404, detail="非法角色名")
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
    try:
        audio_path = get_persona_audio_path(persona_name)
    except ValueError:
        raise HTTPException(status_code=404, detail="非法角色名")
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
    blocked = _managed_block(persona_name)
    if blocked is not None:
        return blocked
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
    blocked = _managed_block(persona_name)
    if blocked is not None:
        return blocked
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
    blocked = _managed_block(persona_name)
    if blocked is not None:
        return blocked
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

    blocked = _managed_block(name)
    if blocked is not None:
        return blocked

    if _is_builtin_persona(name):
        return {
            "status": 1,
            "msg": "默认人格「早柚」的人格卡每次启动从程序写回，不能在这里修改。请先复制一份。",
            "data": None,
        }

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
        from gsuid_core.ai_core.gs_agent import build_new_persona

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

    if _is_builtin_persona(name):
        return {
            "status": 1,
            "msg": "默认人格「早柚」的人格卡每次启动从程序写回，不能在这里修改。请先复制一份。",
            "data": None,
        }

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


@app.post("/api/persona/{persona_name}/copy", summary="复制角色", tags=PERSONA)
async def copy_persona_endpoint(
    persona_name: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """复制整个人格目录。新名称在源名后追加 2/3/…；副本双通道强制 off 并清空目标列表。"""
    persona = Persona(persona_name)
    if not persona.exists():
        return {
            "status": 1,
            "msg": f"角色 '{persona_name}' 不存在",
            "data": None,
        }
    try:
        new_name = await copy_persona(persona_name)
    except ValueError as e:
        return {
            "status": 1,
            "msg": str(e),
            "data": None,
        }
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "name": new_name,
            "source": persona_name,
        },
    }


@app.delete("/api/persona/{persona_name}", summary="删除角色", tags=PERSONA)
async def remove_persona(
    persona_name: str,
    _: Dict[str, Any] = Depends(require_admin),
) -> Dict[str, Any]:
    """
    删除角色

    删除 persona 目录下该角色的整个文件夹。

    Args:
        persona_name: 角色名称

    Returns:
        status: 0成功，1失败
    """
    if _is_builtin_persona(persona_name):
        return {
            "status": 1,
            "msg": "默认人格「早柚」的人格卡每次启动从程序写回，不能删除。",
            "data": None,
        }
    # git 导入的人格删除时连同克隆仓库一起清掉
    from gsuid_core.ai_core.persona.store import uninstall_git_persona

    deleted, msg = await uninstall_git_persona(persona_name)
    if deleted:
        return {
            "status": 0,
            "msg": "ok",
            "data": None,
        }
    else:
        return {
            "status": 1,
            "msg": msg or f"角色 '{persona_name}' 不存在",
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
        data: 配置对象，包含 ai_mode / group_scope / private_scope / target_groups / target_users / scope 等
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


def _parse_int_field(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if text and (text.isdigit() or (text[0] in "+-" and text[1:].isdigit())):
            return int(text)
    return None


def _clean_unique_str_list(raw: list[object]) -> list[str]:
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


@dataclass(slots=True)
class _ParsedPersonaConfigUpdate:
    field_updates: dict[str, str | int | float | bool | list[str]] = field(default_factory=dict)
    meme_mirrors: dict[str, bool | int] = field(default_factory=dict)
    results: list[str] = field(default_factory=list)
    group_scope: str | None = None
    private_scope: str | None = None
    legacy_scope: str | None = None
    ai_mode: list[str] | None = None
    inspect_interval_updated: bool = False


def _parse_persona_config_update(
    persona_name: str,
    data: dict[str, object],
    *,
    takeover_conflicts: bool,
) -> tuple[str | None, _ParsedPersonaConfigUpdate]:
    """整单解析与前置校验：任一字段非法直接返回错误，保证无半写入或提前抢占。"""
    from gsuid_core.ai_core.persona.chat_style import (
        CHAT_STYLE_MAX,
        CHAT_STYLE_MIN,
        SPEECH_LEN_MAX,
        SPEECH_LEN_MIN,
    )
    from gsuid_core.ai_core.persona.meme_style import MEME_FREQUENCY_MAX, MEME_FREQUENCY_MIN
    from gsuid_core.ai_core.prompt_group.store import group_exists
    from gsuid_core.ai_core.persona.config_rules import (
        AI_MODE_OPTIONS,
        CURIOSITY_FOCUS_VALUES,
        normalize_ai_modes,
        autonomy_settings_error,
        autonomy_settings_from_mapping,
    )

    parsed = _ParsedPersonaConfigUpdate()

    raw_group = data["group_scope"] if "group_scope" in data else None
    raw_private = data["private_scope"] if "private_scope" in data else None
    if raw_group is not None or raw_private is not None:
        if raw_group is not None:
            if not isinstance(raw_group, str):
                return "group_scope 必须是字符串", parsed
            parsed.group_scope = raw_group
        if raw_private is not None:
            if not isinstance(raw_private, str):
                return "private_scope 必须是字符串", parsed
            parsed.private_scope = raw_private
        ok, msg = persona_config_manager.validate_channel_scopes(
            persona_name,
            group_scope=parsed.group_scope,
            private_scope=parsed.private_scope,
            takeover_conflicts=takeover_conflicts,
        )
        if not ok:
            return msg, parsed
    elif "scope" in data and data["scope"] is not None:
        raw_scope = data["scope"]
        if not isinstance(raw_scope, str):
            return "scope 必须是字符串", parsed
        parsed.legacy_scope = raw_scope
        ok, msg = persona_config_manager.validate_channel_scopes(
            persona_name,
            legacy_scope=parsed.legacy_scope,
            takeover_conflicts=takeover_conflicts,
        )
        if not ok:
            return msg, parsed

    for key in ("target_groups", "target_users", "enabled_tools", "tool_names", "capability_agents"):
        if key not in data:
            continue
        raw_list = data[key]
        if not isinstance(raw_list, list):
            return f"{key} 必须是列表", parsed
        cleaned = _clean_unique_str_list(raw_list)
        parsed.field_updates[key] = cleaned
        parsed.results.append(f"{key}: {cleaned}")

    if "ai_mode" in data:
        raw_modes = data["ai_mode"]
        if not isinstance(raw_modes, list):
            return "ai_mode 必须是列表", parsed
        modes = normalize_ai_modes([str(item) for item in raw_modes])
        for mode in modes:
            if mode not in AI_MODE_OPTIONS:
                return f"无效的 AI 行动模式: {mode}", parsed
        parsed.field_updates["ai_mode"] = modes
        parsed.ai_mode = modes
        parsed.results.append(f"ai_mode: {modes}")

    for trait_key, low, high in (
        ("social_initiative", 0, 100),
        ("energy_endurance", 0, 100),
        ("arc_persistence", 1, 3),
    ):
        if trait_key not in data:
            continue
        raw_trait = data[trait_key]
        if isinstance(raw_trait, bool) or not isinstance(raw_trait, int):
            return f"{trait_key} 必须是整数", parsed
        if raw_trait < low or raw_trait > high:
            return f"{trait_key} 必须在 {low} 到 {high} 之间", parsed
        parsed.field_updates[trait_key] = raw_trait
        parsed.results.append(f"{trait_key}: {raw_trait}")

    if "curiosity_focus" in data:
        raw_focus = data["curiosity_focus"]
        if not isinstance(raw_focus, str):
            return "curiosity_focus 必须是字符串", parsed
        if raw_focus not in CURIOSITY_FOCUS_VALUES:
            return f"无效的闲时关注点: {raw_focus}", parsed
        parsed.field_updates["curiosity_focus"] = raw_focus
        parsed.results.append(f"curiosity_focus: {raw_focus}")

    if "inspect_interval" in data:
        raw_interval = data["inspect_interval"]
        if isinstance(raw_interval, bool) or not isinstance(raw_interval, int):
            return "inspect_interval 必须是整数", parsed
        if raw_interval not in (5, 10, 15, 30, 60):
            return "无效的巡检间隔，可选值: 5, 10, 15, 30, 60", parsed
        parsed.field_updates["inspect_interval"] = raw_interval
        parsed.inspect_interval_updated = True
        parsed.results.append(f"inspect_interval: {raw_interval}")

    if "keywords" in data:
        raw_kw = data["keywords"]
        if not isinstance(raw_kw, list):
            return "keywords 必须是列表", parsed
        keywords = [str(item) for item in raw_kw]
        parsed.field_updates["keywords"] = keywords
        parsed.results.append(f"keywords: {keywords}")

    autonomy_payload: dict[str, object] = {}
    for autonomy_key in (
        "active_hours",
        "timezone",
        "autonomy_interval_minutes",
        "autonomy_care_min_zone",
    ):
        if autonomy_key in data:
            autonomy_payload[autonomy_key] = data[autonomy_key]
    autonomy_settings, autonomy_error = autonomy_settings_from_mapping(autonomy_payload)
    if autonomy_error:
        return autonomy_error, parsed
    if autonomy_settings is not None:
        settings_err = autonomy_settings_error(autonomy_settings)
        if settings_err:
            return settings_err, parsed
        if autonomy_settings.active_hours is not None:
            parsed.field_updates["active_hours"] = [item.strip() for item in autonomy_settings.active_hours]
        if autonomy_settings.timezone is not None:
            parsed.field_updates["timezone"] = autonomy_settings.timezone.strip()
        if autonomy_settings.interval_minutes is not None:
            parsed.field_updates["autonomy_interval_minutes"] = autonomy_settings.interval_minutes
        if autonomy_settings.care_min_zone is not None:
            parsed.field_updates["autonomy_care_min_zone"] = autonomy_settings.care_min_zone
        parsed.results.append("autonomy")

    for aside_field in (
        "aside_image_probability",
        "aside_text_probability",
        "aside_mixed_probability",
    ):
        if aside_field not in data:
            continue
        raw_prob = data[aside_field]
        if isinstance(raw_prob, bool) or not isinstance(raw_prob, (int, float)):
            return f"{aside_field} 必须是 0 到 1 的数字", parsed
        prob = float(raw_prob)
        if prob < 0 or prob > 1:
            return "命令补充概率必须在 0 到 1 之间", parsed
        parsed.field_updates[aside_field] = prob
        parsed.results.append(f"{aside_field}: {raw_prob}")

    if "aside_allow_tools" in data:
        raw_allow_tools = data["aside_allow_tools"]
        if not isinstance(raw_allow_tools, bool):
            return "aside_allow_tools 必须是布尔值", parsed
        parsed.field_updates["aside_allow_tools"] = raw_allow_tools
        parsed.results.append(f"aside_allow_tools: {raw_allow_tools}")

    if "allow_meme_reply" in data:
        raw_allow_meme = data["allow_meme_reply"]
        if not isinstance(raw_allow_meme, bool):
            return "allow_meme_reply 必须是布尔值", parsed
        parsed.field_updates["allow_meme_reply"] = raw_allow_meme
        parsed.meme_mirrors["allow_meme_reply"] = raw_allow_meme
        parsed.results.append(f"allow_meme_reply: {raw_allow_meme}")

    if "meme_frequency" in data:
        parsed_freq = _parse_int_field(data["meme_frequency"])
        if parsed_freq is None:
            return "meme_frequency 必须是整数", parsed
        if parsed_freq < MEME_FREQUENCY_MIN or parsed_freq > MEME_FREQUENCY_MAX:
            return f"表情频率必须在 {MEME_FREQUENCY_MIN} 到 {MEME_FREQUENCY_MAX} 之间", parsed
        parsed.field_updates["meme_frequency"] = parsed_freq
        parsed.meme_mirrors["meme_frequency"] = parsed_freq
        parsed.results.append(f"meme_frequency: {parsed_freq}")

    if "meme_send_cooldown_sec" in data:
        parsed_cd = _parse_int_field(data["meme_send_cooldown_sec"])
        if parsed_cd is None:
            return "meme_send_cooldown_sec 必须是整数", parsed
        if parsed_cd < 0 or parsed_cd > 3600:
            return "表情发送冷却必须在 0 到 3600 秒之间", parsed
        parsed.field_updates["meme_send_cooldown_sec"] = parsed_cd
        parsed.meme_mirrors["meme_send_cooldown_sec"] = parsed_cd
        parsed.results.append(f"meme_send_cooldown_sec: {parsed_cd}")

    if "prompt_group" in data:
        raw_pg = data["prompt_group"]
        if not isinstance(raw_pg, str):
            return "prompt_group 必须是字符串", parsed
        pg_name = raw_pg.strip()
        if not group_exists(pg_name):
            return "系统提示词组不存在", parsed
        parsed.field_updates["prompt_group"] = pg_name
        parsed.results.append(f"prompt_group: {pg_name}")

    if "chat_style" in data:
        raw_cs = data["chat_style"]
        if isinstance(raw_cs, bool) or not isinstance(raw_cs, int):
            return "chat_style 必须是整数", parsed
        if raw_cs < CHAT_STYLE_MIN or raw_cs > CHAT_STYLE_MAX:
            return f"说话强度必须在 {CHAT_STYLE_MIN} 到 {CHAT_STYLE_MAX} 之间", parsed
        parsed.field_updates["chat_style"] = raw_cs
        parsed.results.append(f"chat_style: {raw_cs}")

    if "speech_len_soft" in data:
        raw_soft = data["speech_len_soft"]
        if isinstance(raw_soft, bool) or not isinstance(raw_soft, int):
            return "speech_len_soft 必须是整数", parsed
        if raw_soft < SPEECH_LEN_MIN or raw_soft > SPEECH_LEN_MAX:
            return f"字数必须在 {SPEECH_LEN_MIN} 到 {SPEECH_LEN_MAX} 之间", parsed
        parsed.field_updates["speech_len_soft"] = raw_soft
        parsed.results.append(f"speech_len_soft: {raw_soft}")

    return None, parsed


def _mirror_meme_settings(persona_name: str, mirrors: dict[str, bool | int]) -> None:
    """将表情三键一次性镜像到 persona.json，保证整包导出两文件一致。"""
    if not mirrors:
        return
    from gsuid_core.utils.plugins_config.models import GsIntConfig, GsBoolConfig

    settings_cfg = persona_settings_manager.get_config(persona_name)
    changed = False
    for key, value in mirrors.items():
        if key not in settings_cfg.config:
            continue
        item = settings_cfg.config[key]
        if isinstance(item, GsBoolConfig) and isinstance(value, bool):
            item.data = value
            changed = True
        elif isinstance(item, GsIntConfig) and isinstance(value, int) and not isinstance(value, bool):
            item.data = value
            changed = True
    if changed:
        settings_cfg.write_config()


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
        data: 配置对象，可包含 ai_mode / group_scope / private_scope / target_groups / target_users / scope /
            takeover_conflicts

    Returns:
        status: 0成功，1失败
        data: 更新后的配置对象

    注意：
    - 推荐使用 group_scope / private_scope (off / specific / all)，两通道全局互斥彼此独立
    - 传 takeover_conflicts=true 时在所有字段校验通过后原子释放冲突人格的对应通道
    - 仍兼容旧客户端仅传 scope (disabled / global / specific / global_group / global_private)
    """
    persona = Persona(persona_name)
    if not persona.exists():
        return {
            "status": 1,
            "msg": f"角色 '{persona_name}' 不存在",
            "data": None,
        }

    # git 人格的行为配置由仓库拥有；启用范围是部署状态，仍然可改
    from gsuid_core.ai_core.persona.store import LOCAL_OVERRIDE_KEYS, is_git_managed

    if is_git_managed(persona_name):
        allowed_git_keys = (*LOCAL_OVERRIDE_KEYS, "takeover_conflicts")
        blocked_keys = [k for k in data if k not in allowed_git_keys]
        if blocked_keys:
            allowed = ", ".join(LOCAL_OVERRIDE_KEYS)
            rejected = ", ".join(sorted(blocked_keys))
            return {
                "status": 1,
                "msg": f"该人格由人格市场管理，仅允许修改 {allowed}；被拒绝的字段: {rejected}",
                "data": None,
            }

    takeover_conflicts = False
    if "takeover_conflicts" in data:
        raw_takeover = data["takeover_conflicts"]
        if not isinstance(raw_takeover, bool):
            return {"status": 1, "msg": "takeover_conflicts 必须是布尔值", "data": None}
        takeover_conflicts = raw_takeover

    # 阶段 1：整单解析与前置校验（全程同步，任一非法直接返回，零半写入）
    validation_error, parsed = _parse_persona_config_update(
        persona_name,
        data,
        takeover_conflicts=takeover_conflicts,
    )
    if validation_error is not None:
        return {"status": 1, "msg": validation_error, "data": None}

    # 阶段 2：先同步完成全部磁盘写入（非通道单次落盘 → 表情镜像 → 通道范围提交），期间不 await
    ok, msg = persona_config_manager.apply_field_updates(persona_name, parsed.field_updates)
    if not ok:
        return {"status": 1, "msg": msg, "data": None}
    _mirror_meme_settings(persona_name, parsed.meme_mirrors)

    results = list(parsed.results)
    if parsed.group_scope is not None or parsed.private_scope is not None:
        success, scope_msg = persona_config_manager.set_channel_scopes(
            persona_name,
            group_scope=parsed.group_scope,
            private_scope=parsed.private_scope,
            takeover_conflicts=takeover_conflicts,
        )
        if not success:
            return {"status": 1, "msg": scope_msg, "data": None}
        if parsed.group_scope is not None:
            results.append(f"group_scope: {parsed.group_scope}")
        if parsed.private_scope is not None:
            results.append(f"private_scope: {parsed.private_scope}")
    elif parsed.legacy_scope is not None:
        success, scope_msg = persona_config_manager.set_scope(
            persona_name,
            parsed.legacy_scope,
            takeover_conflicts=takeover_conflicts,
        )
        if not success:
            return {"status": 1, "msg": scope_msg, "data": None}
        results.append(f"scope: {parsed.legacy_scope}")

    # 阶段 3：落盘全部完成后再启动/重载巡检与自主行动调度器
    if parsed.ai_mode is not None and "定时巡检" in parsed.ai_mode:
        from gsuid_core.ai_core.heartbeat import start_heartbeat_inspector

        start_heartbeat_inspector()
    if parsed.inspect_interval_updated:
        current_modes = persona_config_manager.get_config(persona_name).get_config("ai_mode").data
        if isinstance(current_modes, list) and "定时巡检" in current_modes:
            from gsuid_core.ai_core.heartbeat.inspector import get_inspector

            inspector = get_inspector()
            inspector.stop_for_persona(persona_name)
            inspector.start_for_persona(persona_name)
    if parsed.ai_mode is not None and "自主行动" in parsed.ai_mode:
        from gsuid_core.ai_core.autonomy.scheduler import autonomy_scheduler

        await autonomy_scheduler.start()

    updated_config = persona_config_manager.get_persona_config_dict(persona_name)
    return {
        "status": 0,
        "msg": f"已更新: {', '.join(results)}" if results else "没有更新任何配置",
        "data": updated_config,
    }


@app.get("/api/persona/tools/catalog", summary="工具装配目录（前端选择器用）", tags=PERSONA)
async def get_persona_tool_catalog(
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """人格工具配置页的选择器所需的全部可选项。

    - ``plugins``：进入向量检索池的插件维度（``enabled_tools`` 按此多选）。
    - ``tools``：按插件分组的工具目录，每项带 ``always_mounted``——常驻直装工具
      不经向量检索，前端要能明确区分「默认就有」与「靠检索召回」。
    """
    from gsuid_core.ai_core.agent_node import (
        tool_catalog,
        list_known_plugins,
        list_capability_domains,
    )

    domains = list_capability_domains()
    by_plugin = tool_catalog()

    plugin_items: list[Dict[str, Any]] = [
        {
            "name": name,
            "tool_count": len(tools),
            "always_mounted": sorted(t["name"] for t in tools if t["always_mounted"]),
        }
        for name, tools in sorted(by_plugin.items())
    ]

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "plugins": plugin_items,
            "tools": by_plugin,
            "known_plugins": list_known_plugins(),
            "capability_domains": domains,
        },
    }


def _persona_settings_web_dict(persona_name: str) -> Dict[str, Any]:
    """把 persona.json 序列化成插件配置同构的 ``PluginConfigItem`` 字典。

    表情三键以 config.json 为真值源，展示口径与运行时（``resolve_meme_style``）一致。
    """
    from gsuid_core.ai_core.persona.chat_style import stored_int
    from gsuid_core.ai_core.persona.meme_style import DEFAULT_MEME_FREQUENCY

    cfg = persona_settings_manager.get_config(persona_name)
    p_cfg = persona_config_manager.get_config(persona_name)
    data: Dict[str, Any] = {}
    for key in DEFAULT_PERSONA_SETTINGS:
        item_dict = _build_config_item(cfg.get_config(key), key)
        if key == "allow_meme_reply" and key in p_cfg.config:
            item_dict["value"] = bool(p_cfg.get_config(key).data)
        elif key in ("meme_frequency", "meme_send_cooldown_sec") and key in p_cfg.config:
            fallback = DEFAULT_MEME_FREQUENCY if key == "meme_frequency" else 0
            item_dict["value"] = stored_int(p_cfg, key, fallback)
        data[key] = item_dict
    return data


@app.get("/api/persona/{persona_name}/settings", summary="获取人格设定（persona.json）", tags=PERSONA)
async def get_persona_settings(
    persona_name: str,
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """返回称呼 / 失败短句等 GSC 字段（含 title/desc/type/value），前端按插件配置渲染。"""
    persona = Persona(persona_name)
    if not persona.exists():
        return {
            "status": 1,
            "msg": f"角色 '{persona_name}' 不存在",
            "data": None,
        }
    return {
        "status": 0,
        "msg": "ok",
        "data": _persona_settings_web_dict(persona_name),
    }


@app.put("/api/persona/{persona_name}/settings", summary="更新人格设定（persona.json）", tags=PERSONA)
async def update_persona_settings(
    persona_name: str,
    data: Dict[str, Any],
    _: Dict[str, Any] = Depends(require_auth),
) -> Dict[str, Any]:
    """按 key 写入对应配置值。未知键 / 分割线跳过。也可传完整 PluginConfigItem（取 value）。"""
    blocked = _managed_block(persona_name)
    if blocked is not None:
        return blocked
    persona = Persona(persona_name)
    if not persona.exists():
        return {
            "status": 1,
            "msg": f"角色 '{persona_name}' 不存在",
            "data": None,
        }
    if not isinstance(data, dict):
        return {
            "status": 1,
            "msg": "请求体必须是对象",
            "data": None,
        }
    from gsuid_core.utils.plugins_config.models import GsIntConfig, GsStrConfig, GsBoolConfig

    cfg = persona_settings_manager.get_config(persona_name)
    updated: list[str] = []
    for key, value in data.items():
        if key not in DEFAULT_PERSONA_SETTINGS:
            continue
        template = DEFAULT_PERSONA_SETTINGS[key]
        if isinstance(template, GsDivider):
            continue
        raw: object = value
        if isinstance(value, dict) and "value" in value and "type" in value:
            raw = value["value"]
        val_to_set: object
        if isinstance(template, GsBoolConfig):
            if not isinstance(raw, bool):
                return {"status": 1, "msg": f"{key} 必须是布尔值", "data": None}
            val_to_set = raw
        elif isinstance(template, GsIntConfig):
            if isinstance(raw, bool):
                return {"status": 1, "msg": f"{key} 必须是整数", "data": None}
            try:
                val_to_set = int(raw)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                return {"status": 1, "msg": f"{key} 必须是整数", "data": None}
        elif isinstance(template, GsStrConfig):
            if not isinstance(raw, str):
                return {"status": 1, "msg": f"{key} 必须是字符串", "data": None}
            val_to_set = raw
        else:
            continue
        if key == "allow_meme_reply":
            ok, msg = persona_config_manager.set_allow_meme_reply(persona_name, bool(val_to_set))
            if not ok:
                return {"status": 1, "msg": msg, "data": None}
        elif key == "meme_frequency":
            ok, msg = persona_config_manager.set_meme_frequency(persona_name, int(val_to_set))
            if not ok:
                return {"status": 1, "msg": msg, "data": None}
        elif key == "meme_send_cooldown_sec":
            ok, msg = persona_config_manager.set_meme_send_cooldown_sec(persona_name, int(val_to_set))
            if not ok:
                return {"status": 1, "msg": msg, "data": None}

        if not cfg.set_config(key, val_to_set):
            return {
                "status": 1,
                "msg": f"写入失败: {key}",
                "data": None,
            }
        updated.append(key)
    return {
        "status": 0,
        "msg": f"已更新: {', '.join(updated)}" if updated else "没有更新任何配置",
        "data": _persona_settings_web_dict(persona_name),
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


class PersonaJournalGroupShardItem(TypedDict):
    group_id: str
    content: str
    persistent: List[str]
    monthly: List[str]
    yesterday: List[str]
    month_label: str
    yesterday_label: str
    timeline: List[str]
    timeline_count: int


class PersonaJournalUserShardItem(TypedDict):
    user_id: str
    shard_id: str
    content: str
    persistent: List[str]
    monthly: List[str]
    yesterday: List[str]
    month_label: str
    yesterday_label: str
    timeline: List[str]
    timeline_count: int


class PersonaJournalArchiveRecordItem(TypedDict):
    date: str
    shard: str
    kind: str
    entries: List[str]
    ts: float


class PersonaJournalArchivesData(TypedDict):
    persona_name: str
    selected_month: str
    available_months: List[str]
    records: List[PersonaJournalArchiveRecordItem]


class PersonaJournalArchivesResponse(TypedDict):
    status: int
    msg: str
    data: PersonaJournalArchivesData


class PersonaJournalData(TypedDict):
    persona_name: str
    content: str
    self_content: str
    self_persistent: List[str]
    self_monthly: List[str]
    self_yesterday: List[str]
    self_yesterday_label: str
    self_timeline: List[str]
    standing_summary: str
    persistent_summary: str
    monthly_summary: str
    yesterday_summary: str
    group_shards: List[PersonaJournalGroupShardItem]
    user_shards: List[PersonaJournalUserShardItem]
    archive_months: List[str]
    timeline: List[str]
    timeline_count: int


class PersonaJournalResponse(TypedDict):
    status: int
    msg: str
    data: PersonaJournalData


class PersonaJournalUpdateRequest(BaseModel):
    content: str = ""
    shard_id: str = ""


class PersonaJournalCompactRequest(BaseModel):
    shard_id: str = ""


class PersonaJournalSaveData(TypedDict):
    persona_name: str


class PersonaJournalSaveResponse(TypedDict):
    status: int
    msg: str
    data: PersonaJournalSaveData


class PersonaJournalCompactData(TypedDict):
    persona_name: str
    compacted: bool


class PersonaJournalCompactResponse(TypedDict):
    status: int
    msg: str
    data: PersonaJournalCompactData


@app.get("/api/persona/{persona_name}/journal", summary="获取指定人格的生活手账", tags=PERSONA)
async def get_persona_journal(
    persona_name: str,
    _: SessionRecord = Depends(require_auth),
) -> PersonaJournalResponse:
    """获取指定人格的生活手账全文及解析后的多尺度分片结构。"""
    from gsuid_core.pool import to_thread
    from gsuid_core.ai_core.journal.manager import (
        get_journal_content,
        get_journal_structured,
    )
    from gsuid_core.ai_core.journal.storage import _read_sections

    content = await to_thread(get_journal_content)(persona_name)
    standing, timeline = _read_sections(content)
    structured = await to_thread(get_journal_structured)(persona_name)
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "persona_name": persona_name,
            "content": content,
            "self_content": structured["self_content"],
            "self_persistent": structured["self_persistent"],
            "self_monthly": structured["self_monthly"],
            "self_yesterday": structured["self_yesterday"],
            "self_yesterday_label": structured["self_yesterday_label"],
            "self_timeline": structured["self_timeline"],
            "standing_summary": standing,
            "persistent_summary": structured["persistent_summary"],
            "monthly_summary": structured["monthly_summary"],
            "yesterday_summary": structured["yesterday_summary"],
            "group_shards": structured["group_shards"],
            "user_shards": structured["user_shards"],
            "archive_months": structured["archive_months"],
            "timeline": timeline,
            "timeline_count": len(timeline),
        },
    }


@app.get("/api/persona/{persona_name}/journal/archives", summary="读取指定人格的历史归档手账流水", tags=PERSONA)
async def get_persona_journal_archives(
    persona_name: str,
    month: str = "",
    shard_id: str = "",
    date: str = "",
    _: SessionRecord = Depends(require_auth),
) -> PersonaJournalArchivesResponse:
    """按月/分片/日期读取无损 JSONL 归档流水（含昨日流水与历史冷归档）。"""
    from gsuid_core.pool import to_thread
    from gsuid_core.ai_core.journal.manager import get_journal_archive_records

    records, available_months, selected_month = await to_thread(get_journal_archive_records)(
        persona_name,
        month=month,
        shard_id=shard_id,
        date=date,
    )
    items: List[PersonaJournalArchiveRecordItem] = [
        {
            "date": rec["date"],
            "shard": rec["shard"],
            "kind": rec["kind"],
            "entries": list(rec["entries"]),
            "ts": rec["ts"],
        }
        for rec in records
    ]
    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "persona_name": persona_name,
            "selected_month": selected_month,
            "available_months": available_months,
            "records": items,
        },
    }


@app.put("/api/persona/{persona_name}/journal", summary="保存指定人格的生活手账", tags=PERSONA)
async def update_persona_journal(
    persona_name: str,
    data: PersonaJournalUpdateRequest,
    _: SessionRecord = Depends(require_admin),
) -> PersonaJournalSaveResponse:
    """保存生活手账 Markdown（支持单分片加锁写回或全景写回）。"""
    from gsuid_core.ai_core.journal.manager import save_journal_content_async

    await save_journal_content_async(persona_name, data.content, shard_id=data.shard_id)
    return {
        "status": 0,
        "msg": "ok",
        "data": {"persona_name": persona_name},
    }


@app.post("/api/persona/{persona_name}/journal/compact", summary="整理指定人格的生活手账", tags=PERSONA)
async def compact_persona_journal(
    persona_name: str,
    body: PersonaJournalCompactRequest | None = Body(default=None),
    _: SessionRecord = Depends(require_admin),
) -> PersonaJournalCompactResponse:
    """触发手账整理压缩（支持指定单分片速整，全景手动整理限制同步重度 LLM 次数防网关超时）。"""
    from gsuid_core.ai_core.journal.dream import compact_journal

    req_shard = body.shard_id.strip() if body is not None and body.shard_id else ""
    compacted = await compact_journal(
        persona_name,
        force=True,
        target_shard=req_shard or None,
        max_llm_shards=1 if req_shard else 3,
    )
    return {
        "status": 0,
        "msg": "ok",
        "data": {"persona_name": persona_name, "compacted": compacted},
    }
