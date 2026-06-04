"""
AI Router 模块

负责 AI Session 的路由和管理，包括 Session 创建、路由等功能。
AI 会话对象由 AISessionRegistry 管理，消息历史由通用 message_history 模块管理。

支持:
- 群聊上下文共享 (session_id 绑定到 group_id)
- Persona Prompt 热重载 (检测配置文件修改时间)
"""

from typing import Optional

from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.models import Event

# 通用消息历史模块（用于 session 访问时间维护）
from gsuid_core.message_history import get_history_manager

# AI 会话对象注册表
from gsuid_core.ai_core.session_registry import get_ai_session_registry

from .persona import build_persona_prompt, persona_config_manager
from .gs_agent import GsCoreAIAgent, create_agent
from .resource import PERSONA_PATH
from .persona.group_context import get_group_context

# Persona 文件的 mtime 缓存，用于检测热重载
_persona_mtime_cache: dict[str, float] = {}

# 主人好感度已初始化的 (bot_id, master_id) 集合，避免每次会话重复检查数据库
_master_favorability_checked: set[tuple[str, str]] = set()

# 主人好感度的保底下限与拉升目标值
MASTER_FAVORABILITY_FLOOR = 90
MASTER_FAVORABILITY_TARGET = 95


async def _ensure_master_favorability(bot_id: str) -> None:
    """确保所有主人用户处于高好感度模式。

    主人是机器人的最高权限用户，理应始终被角色亲昵、信任地对待。
    若某个主人的好感度低于保底下限，则自动拉升到目标值。
    每个 (bot_id, master_id) 组合只检查一次。
    """
    from gsuid_core.ai_core.database.models import UserFavorability

    masters = core_config.get_config("masters") or []
    for master_id in masters:
        master_id = str(master_id)
        cache_key = (bot_id, master_id)
        if cache_key in _master_favorability_checked:
            continue
        _master_favorability_checked.add(cache_key)
        try:
            record = await UserFavorability.get_user_favorability(master_id, bot_id)
            current = record.favorability if record else 0
            if current < MASTER_FAVORABILITY_FLOOR:
                # set_favorability 第 4 个参数是 user_name（用户昵称），并非操作者标识。
                # 系统自动初始化时无从得知主人昵称，留空即可，不要误传 master_id。
                await UserFavorability.set_favorability(master_id, bot_id, MASTER_FAVORABILITY_TARGET)
                logger.info(
                    f"🧠 [AI Router] 主人 {master_id} 好感度 {current} 低于下限，已拉升至 {MASTER_FAVORABILITY_TARGET}"
                )
        except Exception as e:
            logger.debug(f"🧠 [AI Router] 主人好感度初始化失败 ({master_id}): {e}")


def _get_persona_mtime(persona_name: str) -> float:
    """获取 persona 配置文件的最新修改时间"""
    persona_dir = PERSONA_PATH / persona_name
    if not persona_dir.exists():
        return 0.0

    newest_mtime = 0.0
    for f in persona_dir.rglob("*"):
        if f.is_file():
            newest_mtime = max(newest_mtime, f.stat().st_mtime)
    return newest_mtime


def _check_persona_changed(session: GsCoreAIAgent, persona_name: str) -> bool:
    """检查 Persona 是否已修改，需要热重载"""
    if session.persona_name != persona_name:
        return True

    current_mtime = _get_persona_mtime(persona_name)
    cached_mtime = _persona_mtime_cache.get(persona_name, 0.0)

    if current_mtime > cached_mtime:
        # Persona 文件已修改，更新缓存
        _persona_mtime_cache[persona_name] = current_mtime
        logger.info(f"🧠 [AI Router] 检测到 Persona '{persona_name}' 已修改，标记需要热重载")
        return True

    return False


async def get_ai_session(event: Event) -> GsCoreAIAgent:
    """获取或创建 AI Session"""
    return await _get_or_create_ai_session(event)


async def get_ai_session_by_id(
    session_id: str,
    user_id: str,
    group_id: Optional[str] = None,
    is_group_chat: bool = False,
) -> Optional[GsCoreAIAgent]:
    """通过 session_id 获取或创建 AI Session"""
    # 从 session_id 构造 Event，保留 WS_BOT_ID / bot_id / bot_self_id，避免 HistoryManager 访问时间更新时 key 不一致。
    from gsuid_core.models import Event

    parts = session_id.split(":", 4)
    if len(parts) != 5:
        return None

    ws_bot_id, bot_id, bot_self_id, target_type, target_id = parts
    if not ws_bot_id or not bot_id or not bot_self_id or not target_id:
        return None
    if target_type == "group":
        parsed_group_id = target_id
        parsed_user_id = ""
        user_type = "group"
    elif target_type == "private":
        parsed_group_id = None
        parsed_user_id = target_id
        user_type = "direct"
    else:
        return None

    ev = Event(
        bot_id=bot_id,
        bot_self_id=bot_self_id,
        user_id=parsed_user_id,
        group_id=parsed_group_id,
        user_type=user_type,
        WS_BOT_ID=ws_bot_id,
    )
    return await _get_or_create_ai_session(ev, session_id=session_id)


async def _get_or_create_ai_session(
    event: Event,
    session_id: Optional[str] = None,
) -> GsCoreAIAgent:
    """内部函数：获取或创建 AI Session 的核心逻辑"""
    if session_id is None:
        session_id = event.session_id

    history_manager = get_history_manager()
    history_manager.update_session_access(event)

    registry = get_ai_session_registry()

    # 主人好感度初始化：确保主人始终处于高好感度模式
    await _ensure_master_favorability(event.bot_id)

    # 检查是否已存在 AI session
    session = registry.get_ai_session(session_id)
    if session is not None:
        persona_name = persona_config_manager.get_persona_for_session(session_id)
        if persona_name and _check_persona_changed(session, persona_name):
            logger.info(f"🧠 [AI Router] 热重载 Session {session_id} 的 Persona '{persona_name}'")
            registry.remove_ai_session(session_id)
            # A-6 修复：热重载只重建 session 还不够——voice_anchor / pre_tool 台词
            # 都是模块级缓存、首次读取后不回盘。同步失效，否则改了 voice_anchor.txt /
            # config.json 的 pre_tool_expressions 必须重启进程才生效。
            from .persona import invalidate_voice_anchor_cache
            from .gs_agent import invalidate_persona_pre_tool_cache

            invalidate_voice_anchor_cache(persona_name)
            invalidate_persona_pre_tool_cache(persona_name)
            session = None
        else:
            return session

    # 创建新 Session
    persona_name = persona_config_manager.get_persona_for_session(session_id)
    if persona_name is None:
        raise ValueError(f"没有为 session {session_id} 配置 persona")

    # 获取群聊上下文（群聊适应性）
    group_description = ""
    if event.group_id:
        group_description = await get_group_context(
            group_id=event.group_id,
        )

    # 情绪隔离 key：群聊用 group_id，私聊用 user_id
    mood_key = event.group_id if event.group_id else event.user_id
    base_persona = await build_persona_prompt(
        persona_name,
        mood_key=mood_key,
        group_description=group_description or None,
    )
    _persona_mtime_cache[persona_name] = _get_persona_mtime(persona_name)

    session = create_agent(
        system_prompt=base_persona,
        persona_name=persona_name,
        create_by="Chat",
        task_level="high",
        session_id=session_id,
    )

    registry.set_ai_session(session_id, session)
    # B-3 修复：函数入口（_get_or_create_ai_session 顶部）已调过一次
    # update_session_access，新建路径这里不再重复刷新访问时间。

    logger.debug(f"🧠 [AI Router] 创建新Session: {session_id}, 使用Persona: {persona_name}")
    return session
