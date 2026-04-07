"""
AI聊天处理模块

处理AI聊天逻辑的独立函数，用于异步队列执行，是全部AI逻辑的入口函数。
支持三种模式：闲聊模式、工具执行模式、问答模式。

设计原则：
- Persona一致性：Session创建时设置base persona，之后保持不变
- 模式指令作为上下文：各模式的特殊要求通过用户消息上下文传递
- 工具按需启用：通过tool_names参数控制是否启用工具调用
"""

from typing import Optional

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.rag import query_knowledge
from gsuid_core.ai_core.utils import prepare_content_payload
from gsuid_core.ai_core.history import get_history_manager
from gsuid_core.ai_core.ai_config import ai_config
from gsuid_core.ai_core.ai_router import (
    get_ai_session,
)
from gsuid_core.ai_core.normalize import normalize_query
from gsuid_core.ai_core.classifier import classifier_service

# AI服务配置开关
enable_ai: bool = ai_config.get_config("enable").data

# 历史记录管理器
history_manager = get_history_manager()


async def handle_ai_chat(bot: Bot, event: Event):
    """
    处理AI聊天逻辑的独立函数，用于异步队列执行，是全部AI逻辑的入口函数

    工作流程：
    1. 获取用户/群组的统一对话Session（包含所有历史聊天记录）
       - Session的system_prompt只在创建时设置一次（base persona），之后保持不变
    2. 意图识别：使用分类器判断用户意图（闲聊/工具/问答）
    3. 根据意图准备上下文：
       - 模式指令作为上下文传递给用户消息，不修改system_prompt
       - 工具模式：RAG检索可用工具，通过tool_names启用
       - 问答模式：RAG检索知识库
    4. 调用Agent生成回复
    5. 发送回复给用户

    Args:
        bot: Bot对象，用于发送消息
        event: Event事件对象，包含用户输入和相关上下文
    """
    if not enable_ai:
        logger.debug("🧠 [GsCore][AI] AI服务未启用，跳过处理")
        return

    try:
        query = event.raw_text

        # 1. 意图识别
        res = await classifier_service.predict_async(query)
        intent = res["intent"]
        logger.debug(f"🧠 [GsCore][AI] 意图识别结果: {res}")

        # 2. 根据意图判断是否启用对应服务
        if intent == "闲聊":
            logger.info("🧠 [GsCore][AI] 闲聊模式")
        elif intent == "工具":
            logger.info("🧠 [GsCore][AI] 工具模式")
        elif intent == "问答":
            logger.info("🧠 [GsCore][AI] 问答模式")

        # 3. 获取Session（Session的system_prompt在创建时已设置，保持不变）
        session = await get_ai_session(event)

        # 4. 根据意图准备上下文
        rag_context: Optional[str] = None  # RAG检索结果

        # 检索知识库作为上下文
        normalized_query = normalize_query(query)
        knowledge_results = await query_knowledge(
            query=normalized_query,
        )

        if knowledge_results:
            context_parts = []
            for r in knowledge_results:
                if r.payload is not None:
                    plugin = r.payload.get("plugin", "unknown")
                    title = r.payload.get("title", "")
                    content = r.payload.get("content", "")
                    context_parts.append(f"[{plugin}] {title}: {content}")
            if context_parts:
                rag_context = "【参考资料】\n" + "\n".join(context_parts)

        # 5. 准备用户消息内容
        user_messages = prepare_content_payload(event)

        # 5.5 获取群聊历史记录并格式化为上下文
        from gsuid_core.ai_core.history import format_history_for_agent

        # 获取最近的历史记录（最多30条）
        # 注意：当前消息已在handler.py中记录到历史，但会通过user_messages单独传递给AI
        # 所以这里获取历史时排除最后一条（即当前消息），避免重复
        raw_history = history_manager.get_history(
            group_id=event.group_id,
            user_id=event.user_id,
            limit=30,
        )

        # 排除最后一条（当前用户刚发的消息），避免与user_messages重复
        history = raw_history[:-1] if raw_history else []

        # 格式化历史记录为Agent可用的上下文格式
        # 标记当前用户，让AI知道是谁在提问
        if history:
            history_context = format_history_for_agent(
                history=history,
                current_user_id=event.user_id,
                current_user_name=event.sender.get("nickname") if event.sender else None,
            )

            # 将历史上下文添加到RAG上下文
            if history_context:
                if rag_context:
                    rag_context = f"【历史对话】\n{history_context}\n\n{rag_context}"
                else:
                    rag_context = f"【历史对话】\n{history_context}"

                logger.debug(f"🧠 [GsCore][AI] 已加载 {len(history)} 条历史消息")

        # 保存当前历史长度，用于后续判断是否有新消息
        history_len_before = len(session.history)

        # 6. 调用Agent生成回复
        chat_result = await session.run(
            user_message=user_messages,
            bot=bot,
            ev=event,
            rag_context=rag_context,
        )

        # 8. 发送回复
        if chat_result:
            await bot.send(chat_result)
            logger.info(f"🧠 [GsCore][AI] 回复已发送 (模式: {intent})")

        # 记录AI回复到历史记录
        # 优先使用 chat_result，如果为空则从 session.history 获取
        reply_content = _extract_text_from_result(chat_result) if chat_result else None

        # 如果 chat_result 为空，尝试从 session.history 获取最后一条 AI 回复
        if not reply_content and len(session.history) > history_len_before:
            # 获取新添加的消息
            new_messages = session.history[history_len_before:]
            logger.debug(f"🧠 [GsCore][AI] 从 session.history 获取 {len(new_messages)} 条新消息")
            # 查找 ModelResponse 类型的消息（AI 回复）
            for msg in reversed(new_messages):
                msg_type = type(msg).__name__
                logger.debug(f"🧠 [GsCore][AI] 检查消息: type={msg_type}")
                # pydantic_ai 使用 ModelResponse 表示 AI 回复
                if msg_type == "ModelResponse":
                    # 尝试从消息中提取文本内容
                    msg_parts = getattr(msg, "parts", None)
                    if msg_parts:
                        text_parts = []
                        for part in msg_parts:
                            part_type = type(part).__name__
                            part_content = getattr(part, "content", None)
                            logger.debug(
                                f"🧠 [GsCore][AI] 检查 part: type={part_type}, has_content={part_content is not None}"
                            )
                            if part_content and part_type == "TextPart":
                                text_parts.append(part_content)
                        if text_parts:
                            reply_content = " ".join(text_parts).strip()
                            logger.debug(f"🧠 [GsCore][AI] 从 parts 提取回复: {reply_content[:50]}...")
                    else:
                        msg_content = getattr(msg, "content", None)
                        if isinstance(msg_content, str):
                            reply_content = msg_content.strip()
                            logger.debug(f"🧠 [GsCore][AI] 从 content 提取回复: {reply_content[:50]}...")
                    if reply_content:
                        break

        if reply_content:
            history_manager.add_message(
                group_id=event.group_id,
                user_id=event.user_id,
                role="assistant",
                content=reply_content,
                metadata={
                    "intent": intent,
                    "bot_id": event.bot_id,
                },
            )

    except Exception as e:
        logger.exception(f"🧠 [GsCore][AI] 聊天异常: {e}")


def _extract_text_from_result(chat_result) -> str:
    """
    从chat_result中提取纯文本内容用于历史记录

    Args:
        chat_result: AI返回的结果，可能是字符串、消息列表或其他格式

    Returns:
        提取的纯文本内容
    """
    if chat_result is None:
        return ""

    # 如果是字符串，直接返回
    if isinstance(chat_result, str):
        return chat_result.strip()

    # 如果是列表（消息段列表），提取文本内容
    if isinstance(chat_result, list):
        text_parts = []
        for item in chat_result:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                # 处理消息段格式 {"type": "text", "data": "..."}
                if item.get("type") == "text" and item.get("data"):
                    text_parts.append(str(item["data"]))
                elif item.get("type") == "at" and item.get("data"):
                    # @某人格式
                    text_parts.append(f"@{item['data']}")
        return " ".join(text_parts).strip()

    # 其他类型，尝试转换为字符串
    try:
        return str(chat_result).strip()
    except Exception:
        return ""
