"""
AI聊天处理模块

处理AI聊天逻辑的独立函数，用于异步队列执行，是全部AI逻辑的入口函数。
支持三种模式：闲聊模式、工具执行模式、问答模式。

设计原则：
- Persona一致性：Session创建时设置base persona，之后保持不变
- 工具按需启用：RAG知识库检索通过主Agent的 search_knowledge 工具按需调用，
                不再作为强制前置流程（避免无谓延迟和Token浪费）
- 双层长度防护：ABSOLUTE_MAX_LENGTH 硬截断，MAX_SUMMARY_LENGTH 智能摘要
- 并发控制：使用全局信号量限制并发AI调用数
"""

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import prepare_content_payload
from gsuid_core.ai_core.history import get_history_manager, format_history_for_agent
from gsuid_core.ai_core.ai_config import ai_config
from gsuid_core.ai_core.ai_router import (
    get_ai_session,
)
from gsuid_core.ai_core.classifier import classifier_service
from gsuid_core.ai_core.statistics import statistics_manager

# AI服务配置开关
enable_ai: bool = ai_config.get_config("enable").data

# 历史记录管理器
history_manager = get_history_manager()

# 双层长度防护配置
ABSOLUTE_MAX_LENGTH = 14000  # 绝对上限：超过此长度直接截断，防止子Agent Token爆炸
MAX_SUMMARY_LENGTH = 8000  # 摘要阈值：超过此长度调用子Agent进行智能摘要（调整至8000避免短文本被过度摘要）


async def handle_ai_chat(bot: Bot, event: Event):
    """
    处理AI聊天逻辑的独立函数，用于异步队列执行，是全部AI逻辑的入口函数

    工作流程：
    1. 双层长度防护：
       - > ABSOLUTE_MAX_LENGTH (14000) → 硬截断，防止子Agent Token爆炸
       - > MAX_SUMMARY_LENGTH (4000) → 调用 create_subagent 智能摘要
    2. 意图识别：使用分类器判断用户意图（闲聊/工具/问答）
    3. 获取 AI Session（含 system_prompt/Persona）
    4. 准备上下文（历史记录）
       - RAG知识库检索不再是强制前置流程
       - 主Agent通过 search_knowledge 工具按需决定是否检索
    5. 调用 Agent 生成回复
    6. 发送回复给用户

    Args:
        bot: Bot对象，用于发送消息
        event: Event事件对象，包含用户输入和相关上下文
    """
    if not enable_ai:
        logger.debug("🧠 [GsCore][AI] AI服务未启用，跳过处理")
        return

    # 从 handler 导入并发控制信号量（避免循环导入）
    from gsuid_core.handler import _ai_semaphore

    async with _ai_semaphore:
        try:
            query = event.raw_text

            # ============================================================
            # 步骤 1: 双层长度防护（D-10 修复）
            # ============================================================
            raw_text_len = len(query)

            if raw_text_len > ABSOLUTE_MAX_LENGTH:
                # 第一层：绝对上限，硬截断，防止把超大文本传给子Agent导致Token爆炸
                logger.warning(f"🧠 [GsCore][AI] 文本超出绝对上限 ({raw_text_len} > {ABSOLUTE_MAX_LENGTH})，执行硬截断")
                query = query[:ABSOLUTE_MAX_LENGTH] + "...[文本过长，已自动截断]"
                event.raw_text = query  # 同步到 event

            # ============================================================
            # 步骤 2: 意图识别
            # ============================================================
            res = await classifier_service.predict_async(query)
            intent = res["intent"]
            logger.debug(f"🧠 [GsCore][AI] 意图识别结果: {res}")

            # 记录意图统计和活跃用户
            statistics_manager.record_intent(intent=intent)
            statistics_manager.record_activity(
                group_id=event.group_id or "private",
                user_id=event.user_id,
                ai_interaction_count=1,
                message_count=1,
            )

            if intent == "闲聊":
                logger.info("🧠 [GsCore][AI] 闲聊模式")
            elif intent == "工具":
                logger.info("🧠 [GsCore][AI] 工具模式")
            elif intent == "问答":
                logger.info("🧠 [GsCore][AI] 问答模式")

            # ============================================================
            # 步骤 3: 获取 AI Session
            # ============================================================
            session = await get_ai_session(event)

            # ============================================================
            # 步骤 4: 准备用户消息
            # ============================================================
            user_messages = prepare_content_payload(event)

            # 第二层：智能摘要（在安全范围内对长文本进行摘要）
            if len(event.raw_text) > MAX_SUMMARY_LENGTH:
                logger.info(f"🧠 [GsCore][AI] 检测到长文本 ({len(event.raw_text)} 字符)，开始摘要...")
                from gsuid_core.ai_core.buildin_tools.subagent import create_subagent

                summarized = await create_subagent(
                    ctx=None,  # type: ignore
                    task=f"请总结以下用户输入，保留关键信息：\n\n{event.raw_text}",
                    tags="摘要,总结",
                    max_tokens=500,
                )
                user_messages = summarized
                logger.info(f"🧠 [GsCore][AI] 摘要完成，摘要长度: {len(summarized)} 字符")

            # ============================================================
            # 步骤 5: 历史记录上下文
            # 注意：RAG 知识库检索已移除为强制前置步骤（D-11 修复）
            # 主Agent通过 search_knowledge 工具按需决定是否检索知识库。
            # 这样可以避免无谓的检索延迟（如用户只是说"你好"时不触发RAG）。
            # ============================================================
            rag_context: str = ""

            # 获取群聊历史记录并格式化为上下文
            # 获取最近的历史记录（最多30条）
            # 注意：当前消息已在 handler.py 中记录到历史，通过 user_messages 单独传递给AI
            # 所以这里获取历史时排除最后一条（即当前消息），避免重复
            raw_history = history_manager.get_history(event, limit=30)

            # 排除最后一条（当前用户刚发的消息），避免与 user_messages 重复
            history = raw_history[:-1] if raw_history else []

            # 格式化历史记录为Agent可用的上下文格式
            if history:
                history_context = format_history_for_agent(
                    history=history,
                    current_user_id=event.user_id,
                    current_user_name=event.sender.get("nickname") if event.sender else None,
                )

                if history_context:
                    rag_context = f"【历史对话】\n{history_context}\n"
                    logger.debug(f"🧠 [GsCore][AI] 已加载 {len(history)} 条历史消息")

            # ============================================================
            # 步骤 6: 调用 Agent 生成回复
            # Agent 会根据对话内容自主决定是否调用 search_knowledge 工具
            # ============================================================
            chat_result = await session.run(
                user_message=user_messages,
                bot=bot,
                ev=event,
                rag_context=rag_context,
            )

            # 步骤 7: 发送回复
            if chat_result:
                await bot.send(chat_result)
                logger.info(f"🧠 [GsCore][AI] 回复已发送 (模式: {intent})")

        except Exception as e:
            logger.exception(f"🧠 [GsCore][AI] 聊天异常: {e}")
