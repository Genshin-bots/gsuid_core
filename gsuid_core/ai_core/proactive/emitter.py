"""统一主动消息发送闭包。

详细设计 / 决策依据：``plans/proactive_message_session_unification_20260529.md`` §3.1。

核心入口 ``emit_proactive_message`` 是**所有"框架在 LLM run 之外注入到用户
会话"的主动输出**唯一出口（Heartbeat 决策播报 / ScheduledTask 任务结果 /
Kanban 子任务转译 / Kanban 失败播报 / 工具主动发送等）。它在内部按序完成：

1. **C8 防撞车**：调 ``UnifiedProactiveDispatcher.should_suppress_heartbeat``。
   语义保留为 "Heartbeat 会被任何其它来源抑制"——其它来源之间不互相抑制，
   避免误杀 ScheduledTask / Kanban 的关键播报。
2. **bot.send**：单次发送；``message_history`` 由 ``_Bot.target_send`` 内部
   写入，metadata 中含 ``proactive=True / proactive_source / trigger_reason``。
   旧 Heartbeat 里"手动再写一次 ``history_manager.add_message``" 的分支
   必须删掉（否则同一条消息会落 message_history 两次）。
3. **主 session 同步**：在用户绑定 ``GsCoreAIAgent`` 中追加一条 assistant-only
   ``ModelMessage``，并在 ``AISessionLogger`` 写一条 ``proactive_emission``
   entry。session 不存在时不做强制创建——主动消息可以"不进 LLM 历史"，
   但日志/网关仍要登记。
4. **C8 网关登记**：``register_send(target_key, source, summary)``——
   旧 Heartbeat / ScheduledTask 是各自手动调，本入口统一调用一次。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pathlib import Path

from gsuid_core.bot import Bot, _Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import send_chat_result
from gsuid_core.ai_core.session_logger import ProactiveSource

# 旧路径仍保留（heartbeat/dispatcher.py），本模块直接复用其单例，
# 不重复实现"防撞车 + 合并语境"语义。
from gsuid_core.ai_core.heartbeat.dispatcher import get_dispatcher

# 老 dispatcher.register_send 兼容字面量集合（plans/proactive_message_session_unification_20260529.md §3.1）
LegacyDispatcherSource = Literal["heartbeat", "task"]


def _resolve_active_bot(event: Event) -> Optional["_Bot"]:
    """从 ``gss.active_bot`` 解析底层 ``_Bot``，与 inspector 兜底逻辑同构。

    优先 ``event.WS_BOT_ID``（最准确）；若 active_bot 完全为空则返回 None。
    """
    from gsuid_core.gss import gss

    if event.WS_BOT_ID and event.WS_BOT_ID in gss.active_bot:
        return gss.active_bot[event.WS_BOT_ID]

    # 兜底：取任意一个可用 _Bot（与 inspector._get_bot_for_session 同样的最末兜底）
    if gss.active_bot:
        return next(iter(gss.active_bot.values()))
    return None


def _target_key(event: Event) -> str:
    """C8 网关目标标识：群聊用 group_id，私聊用 user_id。"""
    if event.group_id:
        return str(event.group_id)
    if event.user_id:
        return str(event.user_id)
    return ""


async def _sync_to_main_session(
    event: Event,
    message: str,
    source: ProactiveSource,
    trigger_reason: str,
    generator_log_files: List[str],
) -> None:
    """把这条主动消息同步进用户绑定的 ``GsCoreAIAgent`` 历史 + session_logger。

    两条路径：
    1. **活跃 session**：主 session 在内存注册表中 → 追加 assistant-only turn +
       proactive_emission entry + link_agent，并立即持久化（避免巡检间隔内
       session 被空闲清理导致 entry 丢失）。
    2. **磁盘回退**：主 session 不在注册表（已被空闲清理 / 用户从未与 AI 说过话）
       → 调 ``AISessionLogger.log_standalone_proactive`` 用临时 logger 走统一的
       会话窗口续写 + _build_data 写盘，格式与活跃 session 一致；窗口内续写既有
       文件，超时 / 从未对话过则新建，保证主动消息不丢失。
    """
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.session_logger import AISessionLogger
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    session_id: str = event.session_id
    if not session_id:
        return
    registry = get_ai_session_registry()
    session: Optional[GsCoreAIAgent] = registry.get_ai_session(session_id)

    if session is None:
        # 主 session 不在内存——走磁盘回退路径。log_standalone_proactive 用一个
        # 临时 logger 复用统一的窗口续写 + _build_data 写盘，格式与活跃 session 一致
        # （取代了旧的手工拼文件的 persist_proactive_emission_to_disk）。
        AISessionLogger.log_standalone_proactive(
            session_id=session_id,
            source=source,
            content=message,
            trigger_reason=trigger_reason,
            generator_log_files=generator_log_files,
        )
        return

    # 链接生成子 agent（决策 / 转译 / 执行体）到主 session 的 linked_agents。
    # 用日志文件的 stem 作为可读的 agent_session_id——它包含原始 session_id +
    # session_uuid + 时间戳，唯一且可回溯。session_uuid 字段在 link_agent 里只
    # 做透传存档，留空即可（webconsole 主要通过 log_file 字段做跳转）。
    # logger 恒在（GsCoreAIAgent 不再有 Optional logger），直接使用。
    for log_file in generator_log_files:
        session._session_logger.link_agent(
            agent_session_id=Path(log_file).stem,
            agent_session_uuid="",
            agent_type="proactive_generator",
            persona_name=session.persona_name,
            create_by=f"Proactive_{source}",
            log_file=log_file,
        )
    # 追加 assistant-only turn + 写 proactive_emission entry
    session.append_proactive_assistant_turn(
        content=message,
        source=source,
        trigger_reason=trigger_reason,
        generator_log_files=generator_log_files,
    )
    # 立即持久化：主动消息写入后立刻落盘，避免 session 被空闲清理时
    # 内存中的 proactive_emission entry 丢失（巡检间隔 30 分钟远大于
    # IDLE_THRESHOLD 30 分钟，entry 很可能在下次持久化前就被清理掉）。
    session._session_logger._persist_sync()


async def emit_proactive_message(
    event: Event,
    message: str,
    *,
    source: ProactiveSource,
    trigger_reason: str,
    generator_log_files: Optional[List[str]] = None,
    bot: Optional[Bot] = None,
    suppress_when_heartbeat_recent: bool = True,
) -> bool:
    """统一主动消息出口。

    Args:
        event: 目标会话（已含 user/group 信息）。
        message: 要发送的文本内容。
        source: 主动消息来源，决定 metadata 分类与是否走 C8 网关防撞车。
        trigger_reason: 触发原因（mood / task_id / subtask display_name 等）。
        generator_log_files: 决策 / 转译子 agent 的 session 日志文件路径列表，
            会作为 ``linked_agents`` 写入用户主 session 的 logger，
            前端可点击跳转。
        bot: 可选；调用方已有 ``Bot`` 实例时直接复用，避免重新构造。
        suppress_when_heartbeat_recent: 仅 Heartbeat 来源需要 ``True``；
            其它来源（task / kanban / tool）默认 ``False``，确保关键播报
            不被刚发完的 Heartbeat 抑制。

    Returns:
        是否真的发送成功。被 C8 抑制 / bot 解析失败时返回 ``False``。
    """
    files: List[str] = list(generator_log_files or [])
    target_key: str = _target_key(event)
    dispatcher = get_dispatcher()

    # 1) C8 防撞车——仅 Heartbeat 自己受抑制；task / kanban / tool 不在乎上次刚发过什么。
    if suppress_when_heartbeat_recent and dispatcher.should_suppress_heartbeat(target_key):
        logger.debug(f"[ProactiveEmitter] C8 抑制 source={source} target={target_key}（近期已有主动输出）")
        return False

    # 2) 解析 Bot 实例
    if bot is None:
        _bot = _resolve_active_bot(event)
        if _bot is None:
            logger.warning(f"[ProactiveEmitter] 无可用 Bot，主动消息发送失败 source={source} target={target_key}")
            return False
        bot = Bot(_bot, event)

    # 3) 实际发送（metadata 通过 extra_metadata 透传到 message_history）。
    #    send_chat_result 的发送侧异常不在这里吞——所有调用 emit_proactive_message
    #    的入口（inspector / executor / kanban_executor / message_sender 工具）
    #    都在更外层有错误捕获并能记录 source 上下文。LLM.md §1.1 禁止在中间层
    #    用 try/except 兜底。
    extra_metadata: Dict[str, Any] = {
        "proactive": True,
        "proactive_source": source,
        "trigger_reason": trigger_reason,
    }
    await send_chat_result(bot, message, ev=event, extra_metadata=extra_metadata)

    # 4) 同步到用户主 session（pydantic_ai 历史 + session_logger）
    await _sync_to_main_session(
        event=event,
        message=message,
        source=source,
        trigger_reason=trigger_reason,
        generator_log_files=files,
    )

    # 5) C8 网关登记——summary 主要给 task 来源用，作为 Heartbeat 合并语境的素材
    #    source 字段为兼容老 dispatcher 仍只接受 "heartbeat" / "task" 两种字面量，
    #    其它来源（kanban / tool）登记成 "task" 走"对 Heartbeat 抑制 + 不进合并语境"。
    legacy_source: LegacyDispatcherSource = "heartbeat" if source == "heartbeat" else "task"
    summary: str = message if source == "scheduled_task" else ""
    dispatcher.register_send(target_key, legacy_source, summary)

    logger.info(f"[ProactiveEmitter] 已发送 source={source} target={target_key} reason={trigger_reason!r}")
    return True
