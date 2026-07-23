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

import re
import time
import asyncio
from typing import Tuple, Optional
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from pydantic_ai.messages import ToolCallPart, ModelResponse

# 导入表情包模块以注册 on_core_shutdown 钩子和 @ai_tools
import gsuid_core.ai_core.meme.startup  # noqa: F401
import gsuid_core.ai_core.buildin_tools.meme_tools  # noqa: F401
from gsuid_core.bot import Bot, _Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import (
    NO_RESULT_TEXT,
    SILENCE_MARKERS,
    ERROR_RESULT_PREFIX,
    send_chat_result,
    classify_error_type,
    prepare_content_payload,
    sanitize_error_for_user,
    has_model_visible_content,
    notify_master_of_agent_error,
    notify_master_of_budget_block,
)
from gsuid_core.message_history import get_history_manager
from gsuid_core.ai_core.gs_agent import STALE_CHAT_REQUEST_TTL
from gsuid_core.ai_core.ai_router import (
    get_ai_session,
)
from gsuid_core.ai_core.classifier import classifier_service
from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.ai_core.persona.mood import update_mood
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.history_format import format_history_for_agent
from gsuid_core.ai_core.database.models import UserFavorability
from gsuid_core.ai_core.context_assembly import fetch_favorability, assemble_dynamic_context
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.buildin_tools.subagent import create_subagent
from gsuid_core.ai_core.memory.retrieval.dual_route import dual_route_retrieve

# 历史记录管理器
history_manager = get_history_manager()

# 双层长度防护配置
ABSOLUTE_MAX_LENGTH = 60000  # 绝对上限：超过此长度直接硬截断，防止子Agent Token爆炸
MAX_SUMMARY_LENGTH = 15000  # 摘要阈值：超过此长度调用子Agent进行智能摘要

# AI并发控制配置
MAX_CONCURRENT_AI_CALLS = 10  # 全局最大并发AI调用数
_ai_semaphore = asyncio.Semaphore(MAX_CONCURRENT_AI_CALLS)  # AI并发信号量

# C4 寒暄门控：回指 / 实体 / 任务引用词，命中则强制触发记忆检索
_FORCE_RETRIEVE_RE = re.compile(
    r"(之前|上次|上回|那个|那次|昨天|前几天|你说过|你不是说|记不记得|还记得|提到过|任务|计划|进度)"
)
# C4 / C3-c：明显情绪词，命中则强制检索（避免错过用户昨日事件背景）
_EMOTION_RETRIEVE_RE = re.compile(r"(难过|崩溃|沉船|破防|开心死|伤心|焦虑|想哭|绝望|委屈|孤独)")
# 可能含实体的特征（英文词 / 引号内容 / 长串中文）
_ENTITY_HINT_RE = re.compile(r"([A-Za-z]{3,}|[「『\"“].+|[一-鿿]{6,})")


def _should_retrieve_memory(query: str, intent: str, user_id: str) -> bool:
    """C4 寒暄门控：判断是否需要触发双路记忆检索（纯规则，无 LLM）。

    只有在"短、闲聊、无实体、无情绪、无回指、非任务引用"同时满足时才跳过；
    主人 / 回指 / 情绪 / 实体一律强制检索，避免漏掉重要背景。
    """
    from gsuid_core.ai_core.utils import _is_master_user

    q = query.strip()
    # 主人：倾向检索
    if _is_master_user(str(user_id)):
        return True
    # 回指 / 任务引用 / 情绪 → 强制检索
    if _FORCE_RETRIEVE_RE.search(q) or _EMOTION_RETRIEVE_RE.search(q):
        return True
    # 仅当短 + 闲聊 + 无实体时跳过双路检索
    if intent == "闲聊" and len(q) < 12 and not _ENTITY_HINT_RE.search(q):
        return False
    return True


def _relevant_preference_contexts(query: str) -> list[str]:
    """选择性偏好注入——按 query 文本匹配本轮可能相关的能力域 / 工具名（叠加 ``general`` 与纠错
    规则由检索侧永远保留）。返回的集合作为 ``dual_route_retrieve(preference_contexts=...)`` 的
    过滤依据，避免无关工具规则每轮都注入、挤占预算并分散工具调用注意力。

    说明：这是 handle_ai 侧按 query 文本的**轻量近似**——能力域多为短中文词（如"文件""定时任务"），
    按子串命中；工具名多为英文，按小写子串命中，覆盖"本轮新意图但工具尚未装配进池"的能力域。
    调用方会再 **∪ gs_agent 上一轮实际装配工具的能力域**（``session.get_assembled_capability_domains()``，
    精确"装配后回传"）后一并作为 ``preference_contexts`` 透传。返回空列表是合法的（表示本轮 query
    未近似匹配到具体能力域，仅纠错 + general 规则、加上回传的装配能力域会注入）。
    """
    matched: set[str] = set()
    try:
        from gsuid_core.ai_core.register import get_registered_tools

        q = query.lower()
        for cat_tools in get_registered_tools().values():
            for name, tb in cat_tools.items():
                dom = tb.capability_domain
                if dom and dom in query:
                    matched.add(dom)
                if name and name.lower() in q:
                    matched.add(name)
    except Exception as e:
        logger.debug(t("🧠 [Memory] 计算偏好相关能力域失败，退化为仅纠错+general: {e}", e=e))
    return list(matched)


async def handle_ai_chat(
    bot: Bot,
    event: Event,
    enqueue_ts: Optional[float] = None,
    soft_triggered: bool = False,
):
    """
    处理AI聊天逻辑的独立函数，用于异步队列执行，是全部AI逻辑的入口函数

    工作流程：
    1. 双层长度防护：
       - > ABSOLUTE_MAX_LENGTH (60000) → 硬截断，防止子Agent Token爆炸
       - > MAX_SUMMARY_LENGTH (15000) → 调用 create_subagent 智能摘要
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
    if not ai_config.get_config("enable").data:
        logger.debug(t("🧠 [GsCore][AI] AI服务未启用，跳过处理"))
        return

    try:
        from gsuid_core.ai_core.startup import is_ai_core_ready, wait_ai_core_ready

        if not is_ai_core_ready():
            logger.info(t("🧠 [GsCore][AI] AI Core 正在初始化/迁移，等待初始化完成后再处理本次消息..."))
            if not await wait_ai_core_ready(timeout=300.0):
                logger.warning(t("🧠 [GsCore][AI] AI Core 初始化等待超时，跳过本次消息以避免查询未完成迁移的向量库"))
                return
    except Exception as e:
        logger.warning(t("🧠 [GsCore][AI] 检查 AI Core 初始化状态失败，继续降级处理: {e}", e=e))

    async with _ai_semaphore:
        # O-A 早退：拿到全局并发信号量时若已排队过久（全局过载场景），话题大概率已翻篇， 直接放弃，
        if enqueue_ts is not None and (time.time() - enqueue_ts) > STALE_CHAT_REQUEST_TTL:
            logger.info(t("🧠 [GsCore][AI] 队列等待 {p0:.1f}s 超 TTL，丢弃过期请求", p0=time.time() - enqueue_ts))
            return
        try:
            query = event.raw_text

            # 预算闸门（被动交互路径·前置短路）：校验 Session Token 额度，超额早退省下后续
            # 记忆/分类/RAG/主 Agent 开销；豁免(主人/白名单)直接放行，check 异常 fail-open。
            budget_decision = None
            try:
                from gsuid_core.ai_core.budget import budget_manager

                budget_decision = await budget_manager.check_scope(
                    str(event.group_id) if event.group_id else "",
                    str(event.user_id),
                    event.bot_id or "",
                    event.session_id,
                )
            except SQLAlchemyError as e:
                logger.warning(t("💰 [GsCore][AI] 预算校验 DB 异常，放行本次消息: {e}", e=e))
            except Exception as e:
                logger.exception(t("💰 [GsCore][AI] 预算校验未知异常，放行本次消息: {e}", e=e))

            if budget_decision is not None and not budget_decision.allowed:
                logger.info(
                    t(
                        "💰 [GsCore][AI] 预算超额拦截 ({p0}): {p1}",
                        p0=budget_decision.block_scope_label,
                        p1=budget_decision.message,
                    )
                )
                if bot is not None:
                    if budget_decision.notify and budget_decision.message:
                        try:
                            await bot.send(budget_decision.message)
                        except Exception as e:
                            logger.warning(t("💰 [GsCore][AI] 预算超额提示发送失败: {e}", e=e))
                    # 主人告警独立于用户提示：即使 notify=False 也让运维感知拦截事件
                    await notify_master_of_budget_block(
                        bot=bot,
                        ev=event,
                        decision=budget_decision,
                    )
                # 提示尽力而为，发送失败也无条件早退，绝不放超额消息进完整 AI 流程。
                return

            # 主动会话：入队触发者原话；被动感知已写过则跳过，防双写
            try:
                _memory_mode = memory_config.memory_mode
                if (
                    ai_config.get_config("enable_memory").data
                    and "主动会话" in _memory_mode
                    and "被动感知" not in _memory_mode
                ):
                    from gsuid_core.ai_core.memory import observe

                    await observe(
                        content=event.raw_text,
                        speaker_id=str(event.user_id),
                        # 私聊 group_id 必须 None，否则会误进 group: scope
                        group_id=str(event.group_id) if event.group_id else None,
                        bot_self_id=str(event.bot_self_id),
                        observer_blacklist=memory_config.observer_blacklist,
                        message_type="group_msg" if event.group_id else "private_msg",
                    )
            except Exception as e:
                logger.debug(t("🧠 [Memory] 主动会话触发者发言入队失败: {e}", e=e))

            # 步骤 1: 双层长度防护（D-10 修复）
            raw_text_len = len(query)

            if raw_text_len > ABSOLUTE_MAX_LENGTH:
                # 第一层：绝对上限，硬截断，防止把超大文本传给子Agent导致Token爆炸
                logger.warning(
                    t(
                        "🧠 [GsCore][AI] 文本超出绝对上限 ({raw_text_len} > {ABSOLUTE_MAX_LENGTH})，执行硬截断",
                        raw_text_len=raw_text_len,
                        ABSOLUTE_MAX_LENGTH=ABSOLUTE_MAX_LENGTH,
                    )
                )
                query = query[:ABSOLUTE_MAX_LENGTH] + "...[文本过长，已自动截断]"
                event.raw_text = query  # 同步到 event

            # 空内容前置门：无可见内容且未@我则静默（与 payload 同源）
            _is_at_me = bool(event.is_tome) or event.user_type == "direct"
            if not query.strip() and not has_model_visible_content(event) and not _is_at_me:
                logger.info(t("🧠 [GsCore][AI] 空内容消息（无模型可见内容且未@我），前置静默跳过"))
                return

            # 步骤 2: 获取 AI Session（意图分类需要上轮是否用过工具）
            session = await get_ai_session(event)

            # 意图：同用户先验 + 近几轮是否真用过工具（勿只看当前句）
            from gsuid_core.ai_core.classifier.mode_classifier import collect_prior_user_turns

            _prev_turn_used_tools = False
            _assistant_seen = 0
            for _msg in reversed(session.history):
                if not isinstance(_msg, ModelResponse):
                    continue
                if any(isinstance(p, ToolCallPart) for p in _msg.parts):
                    _prev_turn_used_tools = True
                    break
                _assistant_seen += 1
                if _assistant_seen >= 6:
                    break

            _hist_for_intent = history_manager.get_history(event, limit=30)
            # handler 已把本轮用户句入库，prior 须去掉与 query 相同的末条
            _prior_user = collect_prior_user_turns(_hist_for_intent, str(event.user_id), max_turns=5)
            _qstrip = query.strip()
            if _prior_user and _prior_user[-1].strip() == _qstrip:
                _prior_user = _prior_user[:-1]
            _prior_user = _prior_user[-4:]
            res = await classifier_service.predict_async(
                query,
                prior_user_turns=_prior_user,
                prev_turn_used_tools=_prev_turn_used_tools,
            )
            intent = res["intent"]
            logger.debug(t("🧠 [GsCore][AI] 意图识别结果: {res}", res=res))

            # 记录意图统计和活跃用户
            statistics_manager.record_intent(intent=intent)
            statistics_manager.record_activity(
                group_id=event.group_id or "private",
                user_id=event.user_id,
                ai_interaction_count=1,
                message_count=1,
            )

            if intent == "闲聊":
                logger.info(t("🧠 [GsCore][AI] 闲聊模式"))
            elif intent == "工具":
                logger.info(t("🧠 [GsCore][AI] 工具模式"))
            elif intent == "问答":
                logger.info(t("🧠 [GsCore][AI] 问答模式"))

            # 软触发沉默门：过门后重置 enqueue_ts，避免门耗时被算进过期 TTL
            if soft_triggered:
                try:
                    from gsuid_core.ai_core.heartbeat.decision import run_reactive_gate

                    gate_history = history_manager.get_history(event, limit=15)
                    if not await run_reactive_gate(event, gate_history, session.persona_name):
                        logger.info(t("🧠 [GsCore][AI] 软触发沉默门判定与AI无关，保持沉默"))
                        return
                    logger.info(t("🧠 [GsCore][AI] 软触发沉默门放行，按续聊处理"))
                except Exception as e:
                    logger.debug(t("🧠 [GsCore][AI] 软触发沉默门异常，放行交主Agent兜底: {e}", e=e))
                if enqueue_ts is not None:
                    enqueue_ts = time.time()

            # 步骤 4: 准备用户消息（含好感度注入）

            # 查询当前用户好感度（从外部存储，非模型推断）
            # Bot.bot_id 是已声明字段；handle_ai 链路 bot 通常非 None
            bot_id = bot.bot_id if bot is not None else ""
            favorability = await fetch_favorability(str(event.user_id), bot_id)

            user_messages = await prepare_content_payload(
                event,
                favorability=favorability,
            )

            # 第二层：智能摘要（在安全范围内对长文本进行摘要）
            # Bug-03修复：摘要时保留上下文头，只替换正文部分
            if len(event.raw_text) > MAX_SUMMARY_LENGTH:
                logger.info(t("🧠 [GsCore][AI] 检测到长文本 ({p0} 字符)，开始摘要...", p0=len(event.raw_text)))

                summarized = await create_subagent(
                    ctx=None,  # type: ignore
                    task=f"请总结以下用户输入，保留关键信息：\n\n{event.raw_text}",
                    max_tokens=18000,
                )
                # 保留上下文头（第一个元素），只替换正文部分
                if isinstance(user_messages, list) and len(user_messages) > 0 and isinstance(user_messages[0], str):
                    # 提取上下文头（--- 消息 ---\n 之前的部分）
                    header_end = user_messages[0].find("--- 消息 ---\n")
                    if header_end != -1:
                        header = user_messages[0][: header_end + len("--- 消息 ---\n")]
                        user_messages[0] = header + summarized + "\n[注：原始消息已摘要]"
                    else:
                        user_messages[0] = summarized
                logger.info(t("🧠 [GsCore][AI] 摘要完成，摘要长度: {p0} 字符", p0=len(summarized)))

            # Bug-04修复：时间注入移到摘要之后（无论是否摘要都需要）
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
            if isinstance(user_messages, list) and len(user_messages) > 0 and isinstance(user_messages[0], str):
                user_messages[0] += f"\n【当前时间】{current_time}"

            # 步骤 5: 记忆上下文（Memory Retrieval）
            # 基于群组/用户ID检索相关记忆，用于个性化响应
            memory_context_text = ""
            is_enable_memory: bool = ai_config.get_config("enable_memory").data
            if is_enable_memory and memory_config.enable_retrieval:
                # C4 寒暄门控：纯寒暄（短+闲聊+无实体/情绪/回指）跳过双路检索， 节省向量搜索 + Reranker 开销；
                if not _should_retrieve_memory(query, intent, str(event.user_id)):
                    logger.debug(t("🧠 [Memory] 命中寒暄门控，跳过双路检索"))
                else:
                    try:
                        # 偏好注入是**能力域过滤**不是整轮开关：闲聊轮传空 contexts，检索侧只留
                        # general/纠错（曾整轮关掉，风格偏好在最该生效的闲聊轮反而被跳过）。
                        _pref_contexts: list[str] = []
                        if intent != "闲聊":
                            # 能力域 = 上轮实际装配工具的域（装配后回传）∪ 本轮 query 子串近似
                            _ctx_set = set(_relevant_preference_contexts(query))
                            _ctx_set.update(session.get_assembled_capability_domains())
                            _pref_contexts = list(_ctx_set)
                        _pref_inject = True
                        mem_ctx = await dual_route_retrieve(
                            query=query,
                            # 私聊必须 None：dual_route 按「group_id 空 → user_global 是主 scope」
                            # 设计，回退成 user_id 只会去查一个空的幻影 group:{user_id}
                            group_id=str(event.group_id) if event.group_id else None,
                            user_id=str(event.user_id),
                            top_k=memory_config.retrieval_top_k,
                            enable_system2=memory_config.enable_system2,
                            enable_user_global=memory_config.enable_user_global_memory,
                            inject_preferences=_pref_inject,
                            preference_contexts=_pref_contexts,
                        )
                        # C4 预算优先级：主人相关记忆优先占用注入预算
                        from gsuid_core.config import core_config

                        masters_set = {str(m) for m in (core_config.get_config("masters") or [])}
                        memory_context_text = mem_ctx.to_prompt_text(
                            max_chars=memory_config.memory_inject_max_chars,
                            priority_speakers=masters_set or None,
                            # §7 第三方隐私拦截：敏感事实仅当事人在场才注入
                            current_speaker_ids={str(event.user_id)},
                        )
                        logger.debug(t("🧠 [Memory] 检索到记忆上下文 ({p0} 字符)", p0=len(memory_context_text)))
                        # 上报记忆检索统计
                        try:
                            statistics_manager.record_memory_retrieval()
                        except Exception:
                            pass
                    except Exception as e:
                        logger.warning(t("🧠 [Memory] 记忆检索失败: {e}", e=e))

            # 步骤 6: 历史记录上下文
            # 注意：RAG 知识库检索已移除为强制前置步骤（D-11 修复）
            rag_context: str = ""

            # 获取群聊历史记录并格式化为上下文
            # 获取最近的历史记录（最多30条）
            raw_history = history_manager.get_history(event, limit=30)

            # 排除最后一条（当前用户刚发的消息），避免与 user_messages 重复
            history = raw_history[:-1] if raw_history else []

            # Fix-06: 当前用户优先的历史窗口过滤
            # 保证当前用户的最近消息一定在窗口内
            if history:
                current_user_id = str(event.user_id)
                CURRENT_USER_MIN_RECORDS = 5  # 当前用户至少保留5条
                MAX_OTHER_RECORDS = 15  # 其他用户最多保留15条

                current_user_records = [r for r in history if r.user_id == current_user_id]
                other_records = [r for r in history if r.user_id != current_user_id]

                # 保留当前用户最近 N 条 + 其他用户最近 M 条，按时间戳重新排序
                selected_current = current_user_records[-CURRENT_USER_MIN_RECORDS:]
                selected_other = other_records[-MAX_OTHER_RECORDS:]

                # 合并并按时间排序
                combined = sorted(selected_current + selected_other, key=lambda r: r.timestamp)
                history = combined

            # 格式化历史记录为Agent可用的上下文格式
            # Bug-05修复: current_user_id 统一 str() 转换，避免类型不一致导致比较失效
            if history:
                history_context = format_history_for_agent(
                    history=history,
                    current_user_id=str(event.user_id),
                    current_user_name=event.sender.get("nickname") if event.sender else None,
                )

                if history_context:
                    rag_context = f"【历史对话】\n{history_context}\n"
                    logger.debug(t("🧠 [GsCore][AI] 已加载 {p0} 条历史消息", p0=len(history)))

            # 动态上下文统一走 assemble_dynamic_context（评测与生产同源）
            _recent_report_titles: Tuple[str, ...] = ()
            for _msg in reversed(session.history):
                if isinstance(_msg, ModelResponse):
                    _meta = _msg.metadata
                    if _meta and "sent_reports" in _meta:
                        _recent_report_titles = tuple(_meta["sent_reports"])
                    break

            mood_key = str(event.group_id) if event.group_id else str(event.user_id)
            full_context, has_actionable = await assemble_dynamic_context(
                query=query,
                user_id=str(event.user_id),
                bot_id=bot_id,
                persona_name=session.persona_name,
                mood_key=mood_key,
                group_id=str(event.group_id) if event.group_id else None,
                favorability=favorability,
                history_context=rag_context,
                memory_context_text=memory_context_text,
                soft_triggered=soft_triggered,
                intent=intent,
                recent_report_titles=_recent_report_titles,
                prev_turn_used_tools=_prev_turn_used_tools,
            )

            # 步骤 7: 调用 Agent 生成回复
            # Agent 会根据对话内容自主决定是否调用 search_knowledge 工具
            chat_result = await session.run(
                user_message=user_messages,
                bot=bot,
                ev=event,
                rag_context=full_context,
                return_mode="by_bot",  # 由 Agent 决定何时通过 bot 发送回复
                enqueue_ts=enqueue_ts,  # O-A 队头阻塞防护：锁级别再判一次 TTL
                intent=intent,  # O-D 意图驱动工具精简
                has_active_task=has_actionable,  # O-D 是否有需要即时介入的 Kanban 任务
            )

            # 步骤 8: 发送回复。结果只分类一次，步骤 9 的好感度门复用同一判定（评审修复 G3）
            result_text = chat_result if isinstance(chat_result, str) else str(chat_result or "")
            _is_silence = bool(result_text) and result_text.strip() in SILENCE_MARKERS
            _is_error = result_text.startswith(ERROR_RESULT_PREFIX) or result_text == NO_RESULT_TEXT
            if chat_result:
                if _is_silence:
                    logger.info(t("🧠 [GsCore][AI] 角色选择沉默，不发送回复"))
                    # 情绪仍然正常更新，只是不发消息
                elif _is_error:
                    # 失败必须让用户可感知，但原始错误串含 provider body 等内部细节，脱敏后发送
                    logger.warning(t("🧠 [GsCore][AI] 本轮执行失败，向用户发送脱敏兜底文案: {r}", r=result_text[:200]))
                    user_facing = sanitize_error_for_user(result_text)
                    try:
                        await send_chat_result(bot, user_facing, ev=event)
                    except Exception as e:
                        logger.warning(t("🧠 [GsCore][AI] 脱敏兜底文案发送失败: {e}", e=e))
                    # 与用户通知解耦：即使发送失败也把详情同步给主人，便于排查
                    await notify_master_of_agent_error(
                        bot=bot,
                        ev=event,
                        error_type=classify_error_type(result_text),
                        result_text=result_text,
                        user_facing=user_facing,
                    )
                else:
                    await send_chat_result(bot, chat_result, ev=event)
                    logger.info(t("🧠 [GsCore][AI] 回复已发送 (模式: {intent})", intent=intent))

            # 情绪与好感：仅有效互动加分（静默/失败不加）
            if session.persona_name:
                mood_key = str(event.group_id) if event.group_id else str(event.user_id)
                from gsuid_core.ai_core.utils import _is_master_user

                # by_bot 成功时 run 返回空串，用 last_run_sent_visible_reply 判说过话
                _effective = not _is_error and (
                    session.last_run_sent_visible_reply or (bool(result_text) and not _is_silence)
                )
                if _effective:
                    await UserFavorability.update_favorability(str(event.user_id), bot.bot_id, 1)

                mood_task = asyncio.create_task(
                    _update_persona_mood(
                        persona_name=session.persona_name,
                        group_id=mood_key,
                        user_message=query,
                        is_master=_is_master_user(str(event.user_id)),
                    )
                )
                # 安全获取底层 _Bot 实例，兼容 Bot 和 MockBot
                # 防止 Bot 继承 _Bot 时 _Bot 分支先匹配导致 underlying 为 Bot 实例
                underlying: _Bot | None = None
                if isinstance(bot, Bot):
                    underlying = bot.bot
                elif isinstance(bot, _Bot):
                    underlying = bot
                elif hasattr(bot, "_real_bot") and isinstance(bot._real_bot, Bot):
                    underlying = bot._real_bot.bot

                if underlying is not None:
                    underlying._add_bg_task(mood_task)
                else:
                    logger.warning(
                        t("🧠 [GsCore][AI] 无法获取 _Bot 实例，mood_task 未被注册到 bg_tasks，可能导致 Task 游离")
                    )

        except Exception as e:
            logger.exception(t("🧠 [GsCore][AI] 聊天异常: {e}", e=e))


async def _update_persona_mood(
    persona_name: str,
    group_id: str,
    user_message: str,
    is_master: bool = False,
) -> None:
    """根据用户消息内容推断情绪事件并更新 Persona 情绪状态

    使用简单的关键词匹配进行情绪事件检测，避免额外的 LLM 调用。

    Args:
        persona_name: Persona 名称
        group_id: 群聊 ID
        user_message: 用户消息内容
        is_master: 当前说话者是否为主人。主人发言会带来额外的正面情绪。
    """
    try:
        text = user_message.lower()

        # 主人发言：带来温暖情绪（与具体内容关键词命中相独立，优先体现）
        if is_master:
            await update_mood(persona_name, group_id, "greeting", 0.35, "主人发言了")

        # 赞美关键词
        praise_keywords = ["可爱", "厉害", "棒", "好强", "喜欢你", "真好", "太帅了", "漂亮", "萌", "赞"]
        # 争执关键词
        argument_keywords = ["讨厌", "烦死了", "闭嘴", "滚", "垃圾", "废物", "白痴"]
        # 伤心事关键词
        sad_keywords = ["难过", "伤心", "哭了", "不开心", "郁闷", "心痛", "分手"]
        # 坏消息关键词
        bad_news_keywords = ["出事了", "出问题了", "报错", "崩了", "挂了", "失败了"]
        # 友好问候关键词
        greeting_keywords = ["你好", "早上好", "晚上好", "嗨", "hi", "hello", "在吗"]
        # 兴奋关键词
        exciting_keywords = ["太棒了", "太好了", "耶", "开心", "中奖了", "成功了"]

        if any(kw in text for kw in praise_keywords):
            await update_mood(persona_name, group_id, "praise", 0.3, "用户赞美")
        elif any(kw in text for kw in argument_keywords):
            await update_mood(persona_name, group_id, "argument", 0.4, "用户争执")
        elif any(kw in text for kw in sad_keywords):
            await update_mood(persona_name, group_id, "sad_news", 0.3, "用户表达伤心")
        elif any(kw in text for kw in bad_news_keywords):
            await update_mood(persona_name, group_id, "bad_news", 0.3, "用户报告坏消息")
        elif any(kw in text for kw in exciting_keywords):
            await update_mood(persona_name, group_id, "exciting", 0.3, "用户表达兴奋")
        elif any(kw in text for kw in greeting_keywords):
            await update_mood(persona_name, group_id, "greeting", 0.2, "用户友好问候")
        else:
            # 普通消息，情绪自然衰减（neutral 会降低当前情绪强度）
            await update_mood(persona_name, group_id, "neutral", 0.05, "")

    except Exception as e:
        logger.debug(t("🎭 [Mood] 情绪更新失败: {e}", e=e))
