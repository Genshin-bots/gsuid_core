"""Core AI 控制管理员命令。"""

from __future__ import annotations

import re
from uuid import uuid4
from typing import Optional
from datetime import datetime

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.message_history import get_history_manager

from .state import ban_scope, get_ban_remaining, set_persona_override

# AI 依赖改函数内懒加载：此插件始终加载，顶层 import 会让 AI 关闭时也付出整套 AI 重栈
sv_core_ai_control = SV("Core AI控制", pm=0)

_DURATION_RE = re.compile(r"(?P<num>\d+)\s*(?P<unit>秒|s|分钟|分|m|小时|时|h|天|日|d)?", re.IGNORECASE)


def _format_seconds(seconds: int) -> str:
    """将秒数格式化为可读时间。"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    if seconds < 3600:
        minutes = seconds // 60
        rest = seconds % 60
        return f"{minutes}分钟" + (f"{rest}秒" if rest else "")
    if seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}小时" + (f"{minutes}分钟" if minutes else "")
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    return f"{days}天" + (f"{hours}小时" if hours else "")


def _parse_duration(text: str, default_seconds: int = 1800) -> int:
    """解析禁言时长，默认 30 分钟。"""
    raw = text.strip()
    if not raw:
        return default_seconds

    match = _DURATION_RE.search(raw)
    if not match:
        return default_seconds

    num = int(match.group("num"))
    unit = (match.group("unit") or "分钟").lower()
    if unit in {"秒", "s"}:
        return num
    if unit in {"小时", "时", "h"}:
        return num * 3600
    if unit in {"天", "日", "d"}:
        return num * 86400
    return num * 60


def _list_persona_names() -> list[str]:
    """列出可用人格名称。"""
    from gsuid_core.ai_core.persona.config import persona_config_manager

    return sorted(persona_config_manager.get_all_configs().keys())


def _resolve_persona_name(text: str) -> Optional[str]:
    """从命令文本中解析人格名称。"""
    target = text.strip()
    if not target:
        return None

    names = _list_persona_names()
    if target in names:
        return target

    lowered = target.lower()
    for name in names:
        if name.lower() == lowered:
            return name
    return None


@sv_core_ai_control.on_command(("clear", "清空会话"), block=True)
async def clear_ai_session(bot: Bot, ev: Event):
    """清空当前 session 的消息历史与 AI 会话对象。"""
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    session_id = ev.session_id
    registry = get_ai_session_registry()
    # 在移除会话（会 close logger、写 session_ended）之前，先往当前会话日志打一个
    # history_reset(user_clear) 标记，供 webconsole 时间线画独立色块区分「用户清空」。
    session = registry.get_ai_session(session_id)
    if session is not None:
        session._session_logger.log_history_reset("user_clear")
    history_deleted = get_history_manager().clear_history(ev)
    ai_deleted = registry.remove_ai_session(session_id)
    logger.info(
        t(
            "[Core AI控制] 清空会话: {session_id}, history={history_deleted}, ai_session={ai_deleted}",
            session_id=session_id,
            history_deleted=history_deleted,
            ai_deleted=ai_deleted,
        )
    )
    await bot.send(await bot.t("✅ [Core AI控制] 已清空当前会话历史，并重置当前 AI Session。"))


@sv_core_ai_control.on_command(("persona", "人格切换"), block=True)
async def switch_persona(bot: Bot, ev: Event):
    """在当前 session 范围内热切换人格。"""
    session_id = ev.session_id
    persona_name = _resolve_persona_name(ev.text)
    if persona_name is None:
        names = _list_persona_names()
        if not ev.text.strip():
            await bot.send(
                "❌ [Core AI控制] 请指定要切换的人格名称。\n可用人格：" + ("、".join(names) if names else "无")
            )
        else:
            await bot.send(
                f"❌ [Core AI控制] 未找到人格：{ev.text.strip()}\n可用人格：" + ("、".join(names) if names else "无")
            )
        return

    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    registry = get_ai_session_registry()
    # 移除会话前先打 history_reset(persona_switch) 标记（带新人格名），供前端时间线区分色块。
    session = registry.get_ai_session(session_id)
    if session is not None:
        session._session_logger.log_history_reset("persona_switch", {"persona_name": persona_name})
    set_persona_override(session_id, persona_name)
    registry.remove_ai_session(session_id)
    logger.info(
        t(
            "[Core AI控制] 当前会话人格热切换: {session_id} -> {persona_name}",
            session_id=session_id,
            persona_name=persona_name,
        )
    )
    await bot.send(
        await bot.t(
            "✅ [Core AI控制] 当前会话已切换人格为「{persona_name}」，后续 AI 配置将按该人格即时生效。",
            persona_name=persona_name,
        )
    )


@sv_core_ai_control.on_command(("btw", "顺便一提"), block=True)
async def run_ephemeral_agent(bot: Bot, ev: Event):
    """创建无人格、无历史的新 Agent 完成本次请求。"""
    task = ev.text.strip()
    if not task:
        await bot.send(await bot.t("❌ [Core AI控制] 请在 btw / 顺便一提 后填写要让新 Agent 完成的内容。"))
        return

    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    session_id = f"btw:{ev.session_id}:{uuid4().hex[:8]}"
    agent = create_agent(
        system_prompt=(
            "你是一个临时任务助手。你没有预设人格，不继承任何历史对话。"
            "只基于用户本次输入完成任务，回答应清晰、准确、直接。"
        ),
        create_by="CoreAIControlBTW",
        task_level="high",
        session_id=session_id,
        is_subagent=True,
    )
    registry = get_ai_session_registry()
    registry.set_ai_session(session_id, agent)
    logger.info(t("[Core AI控制] 启动 BTW 临时 Agent: {session_id}", session_id=session_id))
    try:
        result = await agent.run(
            user_message=task,
            bot=bot,
            ev=ev,
            tools=[],
            return_mode="by_bot",
        )
        if result:
            await bot.send(str(result))
    finally:
        if agent._session_logger is not None:
            agent._session_logger.close()
        registry.remove_ai_session(session_id)


@sv_core_ai_control.on_command(("ban", "禁言"), block=True)
async def ban_ai_scope(bot: Bot, ev: Event):
    """禁止当前范围内 bot 发言与 AI API 调用一段时间。"""
    seconds = _parse_duration(ev.text)
    seconds = max(1, min(seconds, 30 * 86400))
    await bot.send(
        await bot.t(
            "✅ [Core AI控制] 即将禁言当前会话范围 {p0}。期间 Bot 不会在此范围发送消息，也不会触发 AI API。",
            p0=_format_seconds(seconds),
        )
    )
    expire_at = ban_scope(ev.session_id, seconds)
    logger.info(
        t(
            "[Core AI控制] 当前会话范围禁言: {p0}, seconds={seconds}, expire_at={p1}",
            p0=ev.session_id,
            seconds=seconds,
            p1=datetime.fromtimestamp(expire_at),
        )
    )


@sv_core_ai_control.on_command(("ban状态", "禁言状态"), block=True)
async def show_ban_status(bot: Bot, ev: Event):
    """查看当前范围 AI 禁言状态。"""
    remaining = get_ban_remaining(ev.session_id)
    if remaining <= 0:
        await bot.send(await bot.t("✅ [Core AI控制] 当前会话范围未处于禁言状态。"))
        return
    await bot.send(
        await bot.t("⏳ [Core AI控制] 当前会话范围仍处于禁言状态，剩余 {p0}。", p0=_format_seconds(remaining))
    )
