"""
Chat With History API
提供带历史对话的 AI 聊天接口

请求体:
    {
        "user_id": str,           # 用户ID（必填）
        "message": str,            # 当前用户消息（必填）
        "history": [               # 历史对话（可选，默认为空）
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": "..."},
            ...
        ],
        "persona_name": str|None,  # 指定Persona名称（可选，默认使用global配置）
        "bot_id": str,             # Bot ID（可选，默认"HTTP"）
        "group_id": str|None       # 群组ID（可选，私聊时为None）
    }

响应体:
    {
        "status_code": 200,
        "data": "Agent的回复文本"
    }
"""

import asyncio
from typing import Dict

from gsuid_core.bot import _Bot
from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app


@app.post("/api/chat_with_history")
async def chatWithHistory(req: Dict):
    """
    带历史对话的 AI 聊天接口
    """
    from gsuid_core.bot import Bot
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.memory.config import memory_config
    from gsuid_core.ai_core.memory.retrieval.dual_route import dual_route_retrieve

    _bot = _Bot("HTTP")

    user_id = req["user_id"] if "user_id" in req else "http_user"
    logger.info(f"[chat_with_history] received user_id={repr(user_id)}")
    message = req["message"] if "message" in req else ""
    history = req["history"] if "history" in req else []
    persona_name = req["persona_name"] if "persona_name" in req else None
    bot_id = req["bot_id"] if "bot_id" in req else "HTTP"
    group_id = None

    # 请求级别的检索控制参数（可选，默认 None 表示使用全局配置）
    enable_observer_override = req.get("enable_observer")  # None/True/False
    enable_system2_override = req.get("enable_system2")  # None/True/False

    if not message:
        return {"status_code": -101, "data": None, "error": "message is required"}

    try:
        # 根据 user_id / group_id 构建 Event 对象
        # 这使得 Agent 能正确识别会话，支持多用户并发
        from gsuid_core.models import Event

        user_type = "direct"
        event = Event(
            bot_id=bot_id,
            user_id=user_id,
            group_id=None,
            user_type=user_type,
        )
        event.raw_text = message
        event.text = message

        # 将 history 中的 user 消息投入 observe，同步 flush 等待记忆构建完成
        # 优先使用请求级别的 override 值
        _enable_observer = (
            enable_observer_override if enable_observer_override is not None else memory_config.observer_enabled
        )
        if _enable_observer:
            from gsuid_core.ai_core.memory import observe, get_ingestion_worker

            msg_type = "private_msg" if not group_id else "group_msg"
            obs_blacklist = memory_config.observer_blacklist
            bot_self_id_str = bot_id

            for turn in history:
                role = turn["role"] if "role" in turn else ""
                content = turn["content"] if "content" in turn else ""
                if not content:
                    continue

                if role == "user":
                    speaker = str(user_id)
                elif role == "assistant":
                    speaker = f"__assistant_{bot_id}__"
                else:
                    continue

                await observe(
                    content=content,
                    speaker_id=speaker,
                    group_id=group_id,
                    bot_self_id=bot_self_id_str,
                    observer_blacklist=obs_blacklist,
                    message_type=msg_type,
                )

            # 同步等待摄入完成：立即 flush 所有 buffer
            worker = get_ingestion_worker()
            if worker is not None:
                await worker.flush_all()

            # 评测模式下手动触发分层图重建（延迟到全部摄入完成后统一重建）
            if memory_config.eval_mode:
                from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
                from gsuid_core.ai_core.memory.ingestion.hiergraph import rebuild_task

                # 根据 user_id 生成 scope_key 并触发重建
                scope_key = make_scope_key(
                    ScopeType.USER_GLOBAL if not group_id else ScopeType.GROUP,
                    str(user_id) if group_id else str(user_id),
                )
                logger.info(f"🧠 [Memory] 评测模式，手动触发分层图重建 scope_key={scope_key}")
                asyncio.create_task(rebuild_task(scope_key))

        agent = create_agent(
            system_prompt="你是一个智能助手，请根据对话历史回答用户的问题。",
            persona_name=persona_name,
            create_by="TEST",
            max_history=0,
            task_level="high",
        )

        if history:
            from pydantic_ai.messages import TextPart, ModelRequest, ModelResponse, UserPromptPart

            model_messages = []
            for turn in history:
                role = turn["role"] if "role" in turn else ""
                content = turn["content"] if "content" in turn else ""
                if not content:
                    continue

                if role == "user":
                    # 用户消息 -> ModelRequest(parts=[UserPromptPart(...)])
                    model_messages.append(
                        ModelRequest(
                            parts=[UserPromptPart(content=content)],
                        )
                    )
                elif role == "assistant":
                    # 助手回复 -> ModelResponse(parts=[TextPart(...)])
                    model_messages.append(
                        ModelResponse(
                            parts=[TextPart(content=content)],
                        )
                    )

            if model_messages:
                agent.history = model_messages
                agent.extract_history()

        # 构建 RAG 上下文（历史对话 + 长期记忆）
        rag_context = ""

        """
        if history:
            # 将 history 转为 HistoryManager 兼容的 MessageRecord 格式用于格式化
            from gsuid_core.ai_core.history.manager import MessageRecord

            history_records = []
            for msg in agent.history:  # 直接遍历 agent.history
                if isinstance(msg, ModelRequest):
                    for part in cast(list, msg.parts):
                        if isinstance(part, UserPromptPart):
                            content = part.content
                            if isinstance(content, str) and content:
                                history_records.append(
                                    MessageRecord(
                                        role="user",
                                        content=content,
                                        user_id=user_id,
                                    )
                                )
                elif isinstance(msg, ModelResponse):
                    for part in cast(list, msg.parts):
                        if isinstance(part, TextPart) and part.content:
                            history_records.append(
                                MessageRecord(
                                    role="assistant",
                                    content=part.content,
                                    user_id="",
                                )
                            )

            if history_records:
                history_context = format_history_for_agent(
                    history=history_records,
                    current_user_id=user_id,
                )
                if history_context:
                    rag_context = f"{history_context}\n"
        """

        # 构建记忆上下文（基于 user_id / group_id 检索）
        memory_context_text = ""
        if memory_config.enable_retrieval:
            logger.info(f"[dual_route_retrieve] user_id={repr(str(user_id))}")
            # 优先使用请求级别的 override 值
            _enable_system2 = (
                enable_system2_override if enable_system2_override is not None else memory_config.enable_system2
            )
            mem_ctx = await dual_route_retrieve(
                query=message,
                group_id=group_id,
                user_id=str(user_id),
                top_k=memory_config.retrieval_top_k,
                enable_system2=_enable_system2,
                enable_user_global=memory_config.enable_user_global_memory,
            )
            memory_context_text = mem_ctx.to_prompt_text()
            memory_ctx = mem_ctx.to_memory_text()

        if memory_context_text:
            logger.info(f"🧠 [GsCore] 检索到长期记忆: {memory_context_text}")
            rag_context = f"{rag_context}\n【长期记忆】\n{memory_context_text}\n"

        logger.info("启动问答")

        # 调用 Agent（传入 event 和 rag_context）
        result = await agent.run(
            user_message=message,
            bot=Bot(_bot, event),
            ev=event,
            rag_context=rag_context if rag_context else None,
            must_return=True,
        )
        logger.info(result)

        if result:
            return {"status_code": 200, "data": result, "memory": memory_ctx}
        else:
            return {"status_code": -100, "data": None}

    except Exception as e:
        logger.error(f"🧠 [GsCore][chat_with_history] 异常: {e}")
        logger.exception("🧠 [GsCore][chat_with_history] 异常详情:")
        return {"status_code": -102, "data": None, "error": str(e)}
