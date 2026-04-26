"""
自我信息工具模块

提供AI获取自身Persona信息的能力，包括配置、立绘、音频、头像等。
"""

import json
from typing import Literal

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.persona.persona import Persona


@ai_tools(category="buildin")
async def get_self_persona_info(
    ctx: RunContext[ToolContext],
    info_type: Literal["config", "image", "avatar", "audio"],
    persona_name: str,
) -> str:
    """
    获取AI自身Persona的信息

    根据info_type参数返回不同类型的Persona资源信息。
    此工具用于让AI了解自身的基本信息和可用的资源。

    Args:
        ctx: 工具执行上下文（包含bot和ev对象）
        info_type: 信息类型，可选值：
            - "config": 返回config.json配置内容（不含介绍）
            - "image": 返回立绘图片路径
            - "avatar": 返回头像图片路径
            - "audio": 返回音频文件路径
        persona_name: Persona名称，用于指定要查询的 persona

    Returns:
        指定类型的信息内容，格式因info_type而异

    Example:
        >>> await get_self_persona_info(ctx, info_type="config", persona_name="小梦")
        >>> await get_self_persona_info(ctx, info_type="avatar", persona_name="小梦")
        >>> await get_self_persona_info(ctx, info_type="audio", persona_name="小梦")
    """
    persona = Persona(persona_name)

    if info_type == "config":
        # 返回 config.json 配置内容
        config_path = persona.files.persona_dir / "config.json"
        if not config_path.exists():
            return f"⚠️ Persona配置不存在: {config_path}"

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            # 不返回 introduction 字段（那是 persona.md 的内容）
            if "introduction" in config_data:
                del config_data["introduction"]
            return json.dumps(config_data, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"❌ [SelfInfo] 读取config.json失败: {e}")
            return f"⚠️ 读取配置失败: {str(e)}"

    elif info_type == "image":
        # 返回立绘图片路径
        image_path = persona.files.image_path
        if not image_path.exists():
            return f"⚠️ 立绘图片不存在: {image_path}"
        return str(image_path)

    elif info_type == "avatar":
        # 返回头像图片路径
        avatar_path = persona.files.avatar_path
        if not avatar_path.exists():
            return f"⚠️ 头像图片不存在: {avatar_path}"
        return str(avatar_path)

    elif info_type == "audio":
        # 返回音频文件路径
        audio_path = persona.files.get_audio_path()
        if not audio_path or not audio_path.exists():
            return "⚠️ 音频文件不存在"
        return str(audio_path)

    else:
        return f"⚠️ 不支持的信息类型: {info_type}，可选值: config, image, avatar, audio"
