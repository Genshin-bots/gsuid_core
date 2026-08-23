"""
自我信息工具模块

提供AI获取自身Persona信息的能力，包括配置、立绘、音频、头像等。
"""

import json
from typing import Literal

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.utils.resource_manager import RM
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
            - "image": 读取立绘图片并注册到资源管理器，返回可直接用于 edit_image 的资源ID
            - "avatar": 读取头像图片并注册到资源管理器，返回可直接用于 edit_image 的资源ID
            - "audio": 读取音频并注册到资源管理器，返回资源ID
        persona_name: Persona名称，用于指定要查询的 persona

    Returns:
        - "config": JSON字符串
        - "image"/"avatar"/"audio": 资源ID（格式 img_xxxxxxxx），可直接作为
          image_id 传给 edit_image 或 send_message_by_ai

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
            logger.error(t("log.ai.selfinfo_read_config_json", e=e))
            return f"⚠️ 读取配置失败: {str(e)}"

    elif info_type == "image":
        image_path = persona.files.image_path
        if not image_path.exists():
            return f"⚠️ 立绘图片不存在: {image_path}"
        try:
            data = image_path.read_bytes()
            resource_id = RM.register(data)
            logger.debug(t("log.ai.selfinfo_standee_registered_rm_register", resource_id=resource_id))
            return f"{resource_id}（立绘图片，可直接作为 image_id 传给 edit_image）"
        except Exception as e:
            logger.error(t("log.ai.selfinfo_register_standee_rm", e=e))
            return f"⚠️ 立绘读取失败: {e}"

    elif info_type == "avatar":
        avatar_path = persona.files.avatar_path
        if not avatar_path.exists():
            return f"⚠️ 头像图片不存在: {avatar_path}"
        try:
            data = avatar_path.read_bytes()
            resource_id = RM.register(data)
            logger.debug(t("log.ai.selfinfo_avatar_registered_rm_register", resource_id=resource_id))
            return f"{resource_id}（头像图片，可直接作为 image_id 传给 edit_image）"
        except Exception as e:
            logger.error(t("log.ai.selfinfo_register_avatar_rm", e=e))
            return f"⚠️ 头像读取失败: {e}"

    elif info_type == "audio":
        audio_path = persona.files.get_audio_path()
        if not audio_path or not audio_path.exists():
            return "⚠️ 音频文件不存在"
        try:
            data = audio_path.read_bytes()
            resource_id = RM.register(data)
            logger.debug(t("log.ai.selfinfo_audio_registered_rm_register", resource_id=resource_id))
            return f"{resource_id}（音频文件）"
        except Exception as e:
            logger.error(t("log.ai.selfinfo_register_audio_rm", e=e))
            return f"⚠️ 音频读取失败: {e}"

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
    from gsuid_core.ai_core.register import format_capability_family_overview
    from gsuid_core.ai_core.agent_node.registry import format_capability_roster

    ev = ctx.deps.ev
    session_id = ev.session_id if ev else ""

    persona_name = "未知"
    try:
        from gsuid_core.ai_core.persona import persona_config_manager

        if session_id:
            pn = persona_config_manager.get_persona_for_session(session_id)
            if pn:
                persona_name = pn
    except Exception:
        pass

    roster = format_capability_roster() or "（无能力代理）"
    families = format_capability_family_overview(max_families=8, max_chars=600) or "（无工具族）"
    capability_lines = [
        roster,
        families,
        "详情用 capability_map / find_tools。",
    ]

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
        "能力花名册与工具族:",
        *capability_lines,
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


@ai_tools(category="common", capability_domain="自我认知")
async def update_self_note(
    ctx: RunContext[ToolContext],
    content: str,
    note_type: Literal["preference", "commitment", "reflection"] = "preference",
) -> str:
    """记录一条关于你自己的长期信息（写入自我认知演化层）。

    当用户明确表达了对你的称呼偏好、禁忌或长期约束（如"从现在起叫我老板"），
    或你对自己作出了某个承诺、复盘出某个反思时，调用此工具持久化记录。
    从下一轮对话起，这条信息会自动出现在你的自我认知里，无需再次记忆。
    注意：不要为玩笑、临时或不确定的内容调用此工具。

    Args:
        ctx: 工具执行上下文
        content: 要记录的内容，简短一句话即可
        note_type: 记录类型——
            "preference"=学到的偏好（称呼/禁忌等），
            "commitment"=对用户作出的承诺，
            "reflection"=自我复盘反思

    Returns:
        记录结果说明
    """
    from gsuid_core.ai_core.self_cognition import add_self_note

    field_map = {
        "preference": "preferences_learned",
        "commitment": "commitments",
        "reflection": "self_notes",
    }
    # Bot.bot_id 是已声明字段；deps.bot 为 None 时退化为空串走 default scope
    bot_id = ctx.deps.bot.bot_id if ctx.deps.bot is not None else ""

    ok = await add_self_note(bot_id, content, field_map[note_type])
    return "✅ 已记入我的自我认知" if ok else "⚠️ 自我认知记录失败"


@ai_tools(category="buildin", capability_domain="自我认知")
async def query_self_episodes(
    ctx: RunContext[ToolContext],
    limit: int = 5,
) -> str:
    """查询你自己之前说过、做过的事（自我情景记忆）。

    什么时候用：
    - 用户回指你曾经的言行（"你之前说的""你上次答应我的""你不是说…吗"）。
    - 你需要确认自己之前是否承诺/提到过某件事。
    - 当前上下文里找不到答案，但你隐约记得自己之前有过相关言行。

    和 search_cognition 的区别：search_cognition 按 query 联邦检索（含本群记忆）；
    本工具按时间倒序翻 **你自己** 的言行记录（SELF scope 情景片段）。

    Args:
        ctx: 工具执行上下文
        limit: 返回的最近条目数，默认 5

    Returns:
        你最近的言行记录；无记录时会说明。
    """
    from gsuid_core.ai_core.self_cognition import retrieve_self_episodes

    ev = ctx.deps.ev
    bot_self_id = ""
    if ev is not None and ev.bot_self_id:
        bot_self_id = str(ev.bot_self_id)
    elif ctx.deps.bot is not None:
        bot_self_id = str(ctx.deps.bot.bot_self_id or "")
    text = await retrieve_self_episodes(bot_self_id, limit=max(1, min(limit, 10)))
    if not text:
        return "（没有找到我之前的言行记录——可能是还没沉淀，或者确实没说过。）"
    return text
