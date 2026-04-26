from typing import Any, List, Optional
from datetime import datetime

from gsuid_core.logger import logger
from gsuid_core.ai_core.utils import extract_json_from_text
from gsuid_core.ai_core.models import Event
from gsuid_core.ai_core.history import format_history_for_agent
from gsuid_core.ai_core.gs_agent import GsCoreAIAgent, create_agent
from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.utils.database.base_models import async_maker
from gsuid_core.ai_core.memory.ingestion.hiergraph import AIMemHierarchicalGraphMeta

DECISION_PROMPT_TEMPLATE = """
{persona_text}
---

现在你独自看着群里的聊天记录，思考自己要不要说点什么。

【当前时间】
{current_time}

【群里最近发生的事】
{history_context}
{group_summary_section}

---

做决定前，先问自己几件事：

- 现在几点？这个时间点，我这种人会在干嘛？会想开口吗？
- 群里最后一条消息是什么时候发的？现在算冷场吗？
- 大家聊的东西我有没有兴趣？或者有没有人需要我？
- 我上次说话是什么时候？有没有必要再说？

结合自己的性格做判断，不要为了说话而说话。

以严格 JSON 格式输出，禁止包含任何 Markdown 标记：
{{"should_speak": true 或 false, "mood": "此刻角色的内心状态，一句话，用第一人称", "context_hook": "如果决定说话，简述你打算接哪个话头或借什么由头；不说话则留空"}}
"""  # noqa: E501


PROACTIVE_MESSAGE_PROMPT = """
{persona_text}

---

【群里最近发生的事】
{history_context}

【此刻你的状态】
{mood}

---

你决定开口了。
直接输出你想说的话，不要任何前缀、引号或解释。
"""


def _strip_message_quotes(text: str) -> str:
    """去除生成消息首尾可能出现的引号包裹"""
    text = text.strip()
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote) and text.endswith(quote) and len(text) > len(quote) * 2:
            text = text[len(quote) : -len(quote)].strip()
            break
    return text


async def _get_group_summary_for_heartbeat(group_id: str) -> str:
    """获取群组摘要缓存，用于 Heartbeat 决策注入。

    根据 memory_config.enable_heartbeat_memory 配置决定是否返回摘要内容。
    """
    if not memory_config.enable_heartbeat_memory:
        return ""

    if not group_id:
        return ""

    try:
        from sqlmodel import select

        scope_key = make_scope_key(ScopeType.GROUP, group_id)
        async with async_maker() as session:
            result = await session.execute(
                select(AIMemHierarchicalGraphMeta.group_summary_cache).where(
                    AIMemHierarchicalGraphMeta.scope_key == scope_key
                )
            )
            row = result.scalar_one_or_none()
            if row:
                return f"\n\n【群组历史摘要】\n{row}"
    except Exception as e:
        logger.debug(f"🫀 [Heartbeat] 获取群组摘要失败: {e}")

    return ""


async def run_heartbeat(
    event: Event,
    history: List[Any],
    session: GsCoreAIAgent,
) -> Optional[tuple[str, str]]:
    """
    Heartbeat 主入口：决策 + 生成，合并为一次完整流程。

    Returns:
        主动发言内容字符串；若决定不发言或出错则返回 None
    """
    if not history:
        logger.debug("🫀 [Heartbeat] 无历史记录，跳过")
        return None

    persona_text = session.system_prompt
    if not persona_text:
        logger.warning("🫀 [Heartbeat] 无法获取人设文本，跳过")
        return None

    # 两个阶段共用同一份上下文，只格式化一次
    history_context = format_history_for_agent(history=history)
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 获取群组摘要缓存（如果启用）
    group_summary = await _get_group_summary_for_heartbeat(event.group_id or "")

    # ----------------------------------------------------------------
    # 阶段一：决策
    # ----------------------------------------------------------------
    decision_prompt = DECISION_PROMPT_TEMPLATE.format(
        persona_text=persona_text,
        current_time=current_time,
        history_context=history_context,
        group_summary_section=group_summary,
    )

    try:
        _agent = create_agent(
            decision_prompt,
            create_by="Heartbeat_Decision",
        )
        result = await _agent.run(user_message=decision_prompt)

    except Exception as e:
        logger.exception(f"🫀 [Heartbeat] 决策阶段出错: {e}")
        return None

    if not result:
        logger.debug("🫀 [Heartbeat] 决策阶段无返回，跳过")
        return None

    decision = extract_json_from_text(result)
    mood: str = decision["mood"]
    should_speak: bool = decision["should_speak"]

    logger.debug(f"🫀 [Heartbeat] should_speak={should_speak} mood={mood!r} context_hook={decision['context_hook']!r}")

    try:
        statistics_manager.record_trigger(trigger_type="heartbeat")
        statistics_manager.record_heartbeat_decision(
            group_id=event.group_id or "",
            should_speak=should_speak,
        )
    except Exception as e:
        logger.warning(f"📊 [Heartbeat] 记录决策统计失败: {e}")

    if not should_speak:
        logger.debug(f"🫀 [Heartbeat] 🤫 保持沉默: {mood} ({event})")
        return None

    logger.info(f"🫀 [Heartbeat] 💡 决定插话: {mood} ({event})")

    # ----------------------------------------------------------------
    # 阶段二：生成发言
    # ----------------------------------------------------------------
    message_prompt = PROACTIVE_MESSAGE_PROMPT.format(
        persona_text=persona_text,
        history_context=history_context,
        mood=decision["mood"],
    )

    try:
        _agent = create_agent(
            message_prompt,
            create_by="Heartbeat_Output",
        )
        result = await _agent.run(user_message=message_prompt)
    except Exception as e:
        logger.exception(f"🫀 [Heartbeat] 生成阶段出错: {e}")
        return None

    if not result or not result.strip():
        logger.debug("🫀 [Heartbeat] 生成阶段无返回")
        return None

    message = _strip_message_quotes(result)
    logger.info(f"🫀 [Heartbeat] 主动发言: {message!r}")
    return mood, message
