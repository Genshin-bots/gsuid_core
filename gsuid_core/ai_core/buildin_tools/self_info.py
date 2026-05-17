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


@ai_tools(category="common")
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


@ai_tools(category="buildin")
async def get_self_info(ctx: RunContext[ToolContext]) -> str:
    """
    获取自身的完整自我认知信息。

    当用户问"你是谁"、"你能做什么"、"你的主人是谁"，
    或你需要判断某个任务是否在自己能力范围内时，调用此工具。
    返回身份、运行框架、能力边界（可用工具）、主人、当前会话语境等信息。

    Returns:
        结构化的自我认知档案文本
    """
    from gsuid_core.config import core_config
    from gsuid_core.ai_core.register import get_registered_tools

    ev = ctx.deps.ev
    session_id = ev.session_id if ev else ""

    # 当前 Persona 名称
    persona_name = "未知"
    try:
        from gsuid_core.ai_core.persona import persona_config_manager

        if session_id:
            pn = persona_config_manager.get_persona_for_session(session_id)
            if pn:
                persona_name = pn
    except Exception:
        pass

    # 能力边界：按分类汇总已注册工具
    capability_lines: list[str] = []
    try:
        registry = get_registered_tools()
        cat_labels = {
            "self": "核心能力",
            "buildin": "基础工具",
            "common": "常用工具",
            "media": "多媒体",
            "default": "子任务工具",
            "by_trigger": "插件工具",
        }
        for cat, tools in registry.items():
            if not tools:
                continue
            label = cat_labels[cat] if cat in cat_labels else cat
            names = "、".join(list(tools.keys())[:15])
            capability_lines.append(f"  [{label}] {names}")
    except Exception:
        capability_lines.append("  [获取失败]")

    # 主人
    masters = core_config.get_config("masters") or []
    masters_text = "、".join(str(m) for m in masters) if masters else "（未配置）"

    # 当前会话语境
    group_id = ev.group_id if ev else None
    scope_desc = f"群聊 {group_id}" if group_id else "私聊"
    context_tags_text = ""
    try:
        if group_id:
            from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
            from gsuid_core.ai_core.memory.group_profile import get_context_tags

            tags = await get_context_tags(make_scope_key(ScopeType.GROUP, str(group_id)))
            if tags:
                context_tags_text = "、".join(tags)
    except Exception:
        pass

    lines = [
        "【自我认知档案】",
        "",
        "身份基本信息:",
        f"  Persona名称: {persona_name}",
        "  运行框架: GsCore AI Core（PydanticAI Agent 架构）",
        f"  会话ID: {session_id or '未知'}",
        "",
        "我能做到的事（工具能力边界）:",
        *capability_lines,
        "  [说明] 以上工具的具体可用性取决于已安装的插件",
        "",
        "我不能做到的事（诚实边界）:",
        "  - 只能调用已注册的工具，无法直接控制外部系统",
        "  - 无法保证实时信息 100% 准确",
        "",
        f"我的主人（最高权限用户）: {masters_text}",
        "",
        "当前会话:",
        f"  所在场景: {scope_desc}",
    ]
    if context_tags_text:
        lines.append(f"  群组语境: {context_tags_text}")

    return "\n".join(lines)
