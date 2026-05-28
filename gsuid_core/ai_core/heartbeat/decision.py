import json
import time
from typing import Any, List, Optional
from datetime import datetime

from gsuid_core.logger import logger
from gsuid_core.ai_core.utils import SILENCE_MARKERS, extract_json_from_text
from gsuid_core.ai_core.models import Event
from gsuid_core.ai_core.gs_agent import GsCoreAIAgent, create_agent
from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.history_format import format_history_for_agent
from gsuid_core.ai_core.persona.prompts import ROLE_PLAYING_START
from gsuid_core.ai_core.persona.resource import load_persona, extract_compact_persona
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
{group_summary_section}{proactive_merge_section}

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
{proactive_merge_section}
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
    persona_name: str,
    extra_context: str = "",
) -> Optional[tuple[str, str, List[str]]]:
    """
    Heartbeat 主入口：决策 + 生成，合并为一次完整流程。

    Args:
        event:   会话事件
        history: 历史消息列表
        persona_name: 该会话已配置的角色名（由 inspector 直接从
            ``persona_config_manager.get_persona_for_session(session_id)`` 取出，
            **不再通过创建 GsCoreAIAgent 间接拿**——避免每次心跳给"用户从未跟
            AI 说过话"的会话凭空生成一个 2-entry 的空壳 session_logger 文件）。
        extra_context: C8 统一主动网关合并进来的语境（如刚完成的定时任务结果摘要），
            注入决策/发言提示词，让 AI 自然提及而非生硬另起一条播报。

    Returns:
        ``(mood, message, generator_log_files)`` 三元组；若决定不发言或出错返回 None。
        ``generator_log_files`` 是决策 + 发言两个子 agent 的 session 日志路径，
        交给 ``emit_proactive_message`` 挂到主 session 的 ``linked_agents`` 上。
    """
    if not history:
        logger.debug("🫀 [Heartbeat] 无历史记录，跳过")
        return None

    if not persona_name:
        logger.warning("🫀 [Heartbeat] 无法获取角色名称，跳过")
        return None

    # 决策阶段只使用纯人设（角色扮演开始 + 角色资料），
    # 避免完整的 system_prompt 中的工具调用规范、<SILENCE> 规则等执行层约束污染决策。
    persona_content = await load_persona(persona_name)
    if not persona_content:
        logger.warning("🫀 [Heartbeat] 无法加载角色资料，跳过")
        return None

    # 决策阶段用压缩版人格（仅 Identity / Style / Tone / Presence 四要素），
    # 节省每次心跳 ~70% 的 persona token；compact 提取失败则回退完整原文，
    # 保证不会因正则未匹配丢人格描述。完整原文留给后续"生成发言"阶段使用。
    compact_persona = extract_compact_persona(persona_content)
    decision_persona = compact_persona or persona_content
    persona_text = f"{ROLE_PLAYING_START}\n{persona_content}"
    decision_persona_text = f"{ROLE_PLAYING_START}\n{decision_persona}"

    # 两个阶段共用同一份上下文，只格式化一次
    history_context = format_history_for_agent(history=history)
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 获取群组摘要缓存（如果启用）
    group_summary = await _get_group_summary_for_heartbeat(event.group_id or "")

    # C8：统一主动网关合并进来的语境（刚完成的定时任务结果等）
    proactive_merge_section = ""
    if extra_context:
        proactive_merge_section = f"\n\n【你刚完成的事（可自然提及，不必生硬播报）】\n{extra_context}"

    # ----------------------------------------------------------------
    # 阶段一：决策
    # ----------------------------------------------------------------
    decision_prompt = DECISION_PROMPT_TEMPLATE.format(
        persona_text=decision_persona_text,
        current_time=current_time,
        history_context=history_context,
        group_summary_section=group_summary,
        proactive_merge_section=proactive_merge_section,
    )

    # 为决策子 agent 分配独立 session_id 并启用 SubAgent 日志，
    # 让"为什么这一刻决定开口"事后可审计（见 §3.3 Heartbeat 改造）。
    target_for_sid: str = event.group_id or event.user_id or "unknown"
    ts_now: int = int(time.time())
    decision_session_id: str = f"heartbeat_decision_{persona_name}_{target_for_sid}_{ts_now}"
    output_session_id: str = f"heartbeat_output_{persona_name}_{target_for_sid}_{ts_now}"
    generator_log_files: List[str] = []

    decision_agent: GsCoreAIAgent = create_agent(
        decision_prompt,
        create_by="Heartbeat_Decision",
        persona_name=persona_name,
        session_id=decision_session_id,
        is_subagent=True,
    )
    # 用本地变量记住 logger 引用，避免多分支重复读取 _session_logger 的 None 守卫；
    # SubAgent 用完即关，否则 30 分钟巡检间隔会不断堆 logger 后台任务。
    decision_logger = decision_agent._session_logger
    try:
        result: str = await decision_agent.run(user_message=decision_prompt)
    except Exception as e:
        logger.exception(f"🫀 [Heartbeat] 决策阶段出错: {e}")
        if decision_logger is not None:
            generator_log_files.append(str(decision_logger._file_path))
            decision_logger.close()
        return None

    if decision_logger is not None:
        generator_log_files.append(str(decision_logger._file_path))
        decision_logger.close()

    if not result:
        logger.debug("🫀 [Heartbeat] 决策阶段无返回，跳过")
        return None

    # 模型输出 <SILENCE> 或 <end_turn> 表示选择不发言，直接跳过
    if result.strip() in SILENCE_MARKERS:
        logger.debug("🫀 [Heartbeat] 模型输出沉默标记，保持沉默")
        return None

    try:
        decision = extract_json_from_text(result)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"🫀 [Heartbeat] 决策结果 JSON 解析失败: {e}, raw={result!r}")
        return None
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
        proactive_merge_section=proactive_merge_section,
    )

    output_agent: GsCoreAIAgent = create_agent(
        message_prompt,
        create_by="Heartbeat_Output",
        persona_name=persona_name,
        session_id=output_session_id,
        is_subagent=True,
    )
    output_logger = output_agent._session_logger
    try:
        result = await output_agent.run(user_message=message_prompt)
    except Exception as e:
        logger.exception(f"🫀 [Heartbeat] 生成阶段出错: {e}")
        if output_logger is not None:
            generator_log_files.append(str(output_logger._file_path))
            output_logger.close()
        return None

    if output_logger is not None:
        generator_log_files.append(str(output_logger._file_path))
        output_logger.close()

    if not result or not result.strip():
        logger.debug("🫀 [Heartbeat] 生成阶段无返回")
        return None

    message: str = _strip_message_quotes(result)
    logger.info(f"🫀 [Heartbeat] 主动发言: {message!r}")
    return mood, message, generator_log_files
