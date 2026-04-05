"""
Persona APIs
提供 AI Persona 角色相关的 RESTful APIs
包括列出所有角色、获取角色详情和上传角色头像功能
"""

import base64
from typing import Dict

from fastapi import Depends

from gsuid_core.data_store import AI_CORE_PATH, get_res_path
from gsuid_core.ai_core.persona import (
    load_persona,
    save_persona,
    delete_persona,
    build_new_persona,
    list_available_personas,
)
from gsuid_core.webconsole.app_app import app
from gsuid_core.webconsole.web_api import require_auth

# 角色存储路径
PERSONA_PATH = get_res_path(AI_CORE_PATH / "persona")


@app.get("/api/persona/list")
async def get_persona_list(_: Dict = Depends(require_auth)) -> Dict:
    """
    获取所有可用的 Persona 角色列表

    Returns:
        status: 0成功，1失败
        data: 角色名称列表
    """
    personas = list_available_personas()
    return {
        "status": 0,
        "msg": "ok",
        "data": personas,
    }


@app.get("/api/persona/{persona_name}")
async def get_persona_detail(persona_name: str, _: Dict = Depends(require_auth)) -> Dict:
    """
    获取指定 Persona 角色的详细信息

    Args:
        persona_name: 角色名称

    Returns:
        status: 0成功，1失败
        data: 角色详情内容（Markdown格式）
    """
    try:
        content = await load_persona(persona_name)
        return {
            "status": 0,
            "msg": "ok",
            "data": {
                "name": persona_name,
                "content": content,
            },
        }
    except FileNotFoundError:
        return {
            "status": 1,
            "msg": f"角色 '{persona_name}' 不存在",
            "data": None,
        }


@app.post("/api/persona/{persona_name}/avatar")
async def upload_persona_avatar(
    persona_name: str,
    data: Dict,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    上传角色头像图片

    将 Base64 编码的 PNG 图片保存到 persona 目录下，文件名为角色名.png

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

    # 确保目录存在
    if not PERSONA_PATH.exists():
        PERSONA_PATH.mkdir(parents=True, exist_ok=True)

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

    # 保存文件
    avatar_path = PERSONA_PATH / f"{persona_name}.png"
    with open(avatar_path, "wb") as f:
        f.write(image_data)

    return {
        "status": 0,
        "msg": "ok",
        "data": {
            "path": str(avatar_path.absolute()),
        },
    }


@app.post("/api/persona/create")
async def create_persona(
    data: Dict,
    _: Dict = Depends(require_auth),
) -> Dict:
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


@app.delete("/api/persona/{persona_name}")
async def remove_persona(
    persona_name: str,
    _: Dict = Depends(require_auth),
) -> Dict:
    """
    删除角色

    删除 persona 目录下的角色名.md 和角色名.png 文件。

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
