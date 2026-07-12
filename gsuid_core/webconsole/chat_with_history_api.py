"""
Chat With History API
提供带历史对话的 AI 聊天接口（请求字段见 ``ChatWithHistoryRequest``）。

响应体:
    {
        "status_code": 200,
        "data": "Agent的回复文本"
    }
"""

import asyncio
from typing import List, Union, Optional

from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from gsuid_core.bot import _Bot
from gsuid_core.logger import logger
from gsuid_core.webconsole.app_app import app
from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
from gsuid_core.ai_core.memory.observer import parse_iso_or_unix_timestamp
from gsuid_core.webconsole._local_test_gate import LOCAL_TEST_MODE, require_local_test
from gsuid_core.ai_core.memory.ingestion.hiergraph import rebuild_task

from ._api_tags import CHAT


class ChatHistoryTurn(BaseModel):
    """单条历史对话。role 仅识别 user/assistant，其余忽略。"""

    model_config = ConfigDict(extra="allow")

    role: str = ""
    content: str = ""
    timestamp: Union[str, int, float, None] = None


class ChatWithHistoryRequest(BaseModel):
    """带历史对话的聊天请求（schema 化，替代裸 Dict 的 docstring 约定——C-6）。"""

    user_id: str = "http_user"
    message: str = ""
    history: List[ChatHistoryTurn] = []
    persona_name: Optional[str] = None
    bot_id: str = "HTTP"
    group_id: Optional[str] = None
    enable_tools: bool = False  # 装配真实工具集（agent 能力评测用）
    max_history: int = 0  # 喂进模型上下文的历史条数；0=仅走记忆检索
    enable_observer: Optional[bool] = None  # None=沿用全局配置
    enable_system2: Optional[bool] = None  # None=沿用全局配置
    trigger_rebuild: bool = False  # 显式触发分层图重建（与 batch_observe 对齐）


@app.post("/api/chat_with_history", include_in_schema=LOCAL_TEST_MODE, summary="带历史的对话", tags=CHAT)
async def chatWithHistory(
    req: ChatWithHistoryRequest,
    _gate: Optional[None] = Depends(require_local_test),
):
    """
    带历史对话的 AI 聊天接口（仅本地测试，默认 404）。
    """
    from gsuid_core.bot import Bot
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.memory.config import memory_config
    from gsuid_core.ai_core.memory.retrieval.dual_route import dual_route_retrieve

    _bot = _Bot("HTTP")

    user_id = req.user_id
    logger.info(f"[chat_with_history] received user_id={repr(user_id)}")
    message = req.message
    history = req.history
    persona_name = req.persona_name
    bot_id = req.bot_id
    group_id = None

    # 请求级别的检索控制参数（None 表示使用全局配置）
    enable_observer_override = req.enable_observer
    enable_system2_override = req.enable_system2
    trigger_rebuild = req.trigger_rebuild

    if not message:
        return {"status_code": -101, "data": None, "error": "message is required"}

    # 输入侧安全防线（与生产 handle_event 路径一致）：伪造工具返回降权 + 编码型注入中和。
    # 此端点原先直传 raw message 绕过了它——安全控制须作用于所有入口。受 content_guard_enable 控。
    # 只标注喂给 Agent 的文本；raw message 保留给记忆检索 query / event（与生产管线的作用点一致）。
    from gsuid_core.ai_core.content_guard import annotate_untrusted_message
    from gsuid_core.ai_core.configs.ai_config import ai_config

    _guard_on = bool(ai_config.get_config("content_guard_enable").data)
    agent_message = annotate_untrusted_message(message) if _guard_on else message

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
                role = turn.role
                content = turn.content
                if not content:
                    continue

                if role == "user":
                    speaker = str(user_id)
                elif role == "assistant":
                    speaker = f"__assistant_{bot_id}__"
                else:
                    continue

                # 评测侧 turn.timestamp → ISO8601 / Unix；非 str/数字内部已返回 None
                ts_parsed = parse_iso_or_unix_timestamp(turn.timestamp)

                await observe(
                    content=content,
                    speaker_id=speaker,
                    group_id=group_id,
                    bot_self_id=bot_self_id_str,
                    observer_blacklist=obs_blacklist,
                    message_type=msg_type,
                    timestamp=ts_parsed,
                )

            # 同步等待摄入完成：立即 flush 所有 buffer
            worker = get_ingestion_worker()
            if worker is not None:
                await worker.flush_all()

            # 评测模式或请求显式 trigger_rebuild 时手动触发分层图重建
            if memory_config.eval_mode or trigger_rebuild:
                scope_key = make_scope_key(
                    ScopeType.USER_GLOBAL if not group_id else ScopeType.GROUP,
                    str(group_id) if group_id else str(user_id),
                )
                logger.info(f"🧠 [Memory] 手动触发分层图重建 scope_key={scope_key}")
                asyncio.create_task(rebuild_task(scope_key))

        # 评测侧可显式要求装配真实工具集（agent 能力评测用）；默认 None 保持记忆评测的
        # 无工具行为不变（非破坏性）。dynamic_tools=True → gs_agent 走 L1–L5 真实工具装配。
        _enable_tools = req.enable_tools
        # 默认 0 = 记忆评测原行为（历史走 observe→记忆检索，不进上下文）；agent 评测传正值
        # 让端点把请求 history 真正喂进模型上下文（否则 extract_history 在 max_history<=0 时清空）。
        _max_history = req.max_history
        # 指定 persona 时用其真实人设 system_prompt；不指定则通用助手。
        # 装配与生产 ai_router **同源**（context_assembly.build_session_system_prompt：
        # persona + 稳定前缀；本端点无群故无群简介/群画像块）——评测测到的 system prompt
        # 结构 = 生产结构（§5.3 装配统一）。
        _sys_prompt = "你是一个智能助手，请根据对话历史回答用户的问题。"
        if persona_name:
            from gsuid_core.ai_core.persona.persona import Persona
            from gsuid_core.ai_core.context_assembly import build_session_system_prompt

            # 不存在的 persona 名回退通用助手：load_persona 会抛 FileNotFoundError，
            # 一个拼写错误就让整个请求 -102、评测整批看起来像 core 挂了
            if Persona(persona_name).exists() or persona_name == "智能助手":
                _sys_prompt = await build_session_system_prompt(event, persona_name)
            else:
                logger.warning(f"[chat_with_history] persona '{persona_name}' 不存在，回退通用助手提示词")
        agent = create_agent(
            system_prompt=_sys_prompt,
            persona_name=persona_name,
            create_by="TEST",
            max_history=_max_history,
            task_level="high",
            session_id=f"test_{user_id}",
            dynamic_tools=True if _enable_tools else None,
        )

        if history:
            from pydantic_ai.messages import TextPart, ModelRequest, ModelResponse, UserPromptPart

            # 生产管线里每条用户消息都过 annotate_untrusted_message（伪造工具返回/编码注入
            # 降权）；请求注入的 history 须同样标注，保持防线对齐。_guard_on 已在入口算好。
            model_messages = []
            for turn in history:
                role = turn.role
                content = turn.content
                if not content:
                    continue

                if role == "user":
                    if _guard_on:
                        content = annotate_untrusted_message(content)
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

        # 构建记忆上下文（基于 user_id / group_id 检索）
        # 提前初始化以保证 enable_retrieval=False 分支下 memory_ctx 不为 unbound
        memory_context_text = ""
        memory_ctx = ""
        if memory_config.enable_retrieval:
            logger.info(f"[dual_route_retrieve] user_id={user_id}")
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
            # 必须传入配置的注入预算：默认 max_chars=2000 只够 ~2 条 Episode，长对话回灌评测
            # 下绝大多数事实落在预算外（与 handle_ai 对齐，由 memory_inject_max_chars 统一控制）。
            memory_context_text = mem_ctx.to_prompt_text(max_chars=memory_config.memory_inject_max_chars)
            memory_ctx = mem_ctx.to_memory_text()

        mem_guide = ""
        if memory_context_text:
            # 只记摘要，不落全文：注入文本可达 30k+ 字符，全文进日志会撑爆内存日志缓冲
            logger.info(f"🧠 [GsCore] 检索到长期记忆: {len(memory_context_text)} chars: {memory_context_text[:300]}...")
            # 记忆使用准则（通用 memory-agent 行为，非针对性）：片段均带时间戳，回答时
            # ① 同一属性有多个取值时以时间最新者为准；② 发现用户前后陈述矛盾要指出矛盾并请
            # 其澄清，而非径直选一个；③ 优先引用记忆中的具体数字/版本/日期，不要泛泛而谈。
            mem_guide = (
                "[Memory-usage guidelines] The fragments below are timestamped. When answering:\n"
                "1) For a question about a CURRENT/latest value where the same attribute has several "
                "values over time, the user UPDATED it — answer with the MOST RECENT value; don't list "
                "the historical ones. (This applies to a single attribute, NOT to summing/combining "
                "figures from different projects/sources — there, use each source's relevant figure. "
                "When summing, first check whether one figure is ALREADY a combined total covering the "
                "others; if so report that total instead of double-counting.)\n"
                "2) If the user made directly CONTRADICTORY statements (e.g. 'I always do X' vs 'I never "
                "do X'), explicitly state that there is contradictory information and ask them to "
                "clarify; do NOT silently pick one or downplay it as an exception.\n"
                "3) Quote the exact number/version/date/price from the fragments; don't paraphrase.\n"
                "3b) Dates on 【核心事实】 lines and timestamps on 【相关对话片段】 are both STATEMENT "
                "times (when the user actually said it). Use them directly to decide which value is "
                "'latest'; when a fact line and a conversation fragment disagree about the same "
                "attribute, prefer the source with the later statement time.\n"
                "4) If memory genuinely lacks the SPECIFIC thing asked, plainly say there is no such "
                "information; don't pad with loosely-related content or speculate.\n"
                "5) Do not infer a PERSON's background, qualifications or role solely from the "
                "assistant's own past suggestions/praise (e.g. 'choose experienced reviewers like X' "
                "does not establish X's expertise); for such personal attributes require an explicit "
                "user statement, otherwise say the information is not available. All other content "
                "(plans, numbers, task details) counts as evidence regardless of speaker.\n"
                "6) When the user asks HOW to do a task (structure a calculation, write code, plan "
                "something), ground your answer in THEIR remembered specifics — their actual providers, "
                "prices, versions, latency/throughput targets from the fragments — as the working values, "
                "instead of inventing placeholder numbers or generic examples.\n"
                "7) Reply in the same language as the user's question.\n"
            )

        # 每轮动态上下文与生产同源装配（情绪/关系行/口吻锚点/自我情景/长任务/长期记忆）：
        # 顺序唯一定义在 assemble_dynamic_context，handle_ai 消费同一函数（§5.3）。
        # 评测历史走 agent.history（上方已喂），故 history_context 传空。
        from gsuid_core.ai_core.context_assembly import fetch_favorability, assemble_dynamic_context

        _favor = await fetch_favorability(str(user_id), bot_id)
        rag_context, _ = await assemble_dynamic_context(
            query=message,
            user_id=str(user_id),
            bot_id=bot_id,
            persona_name=persona_name,
            mood_key=str(user_id),
            favorability=_favor,
            history_context="",
            memory_context_text=memory_context_text,
            memory_guide=mem_guide,
        )

        logger.info("启动问答")

        # 调用 Agent（传入 event 和 rag_context）
        result = await agent.run(
            user_message=agent_message,
            bot=Bot(_bot, event),
            ev=event,
            rag_context=rag_context if rag_context else None,
            return_mode="return",
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
