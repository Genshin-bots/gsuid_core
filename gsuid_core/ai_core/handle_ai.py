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
import asyncio
from typing import Optional
from datetime import datetime

# 导入表情包模块以注册 on_core_shutdown 钩子和 @ai_tools
import gsuid_core.ai_core.meme.startup  # noqa: F401
import gsuid_core.ai_core.buildin_tools.meme_tools  # noqa: F401
from gsuid_core.bot import Bot, _Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import SILENCE_MARKERS, send_chat_result, prepare_content_payload
from gsuid_core.message_history import get_history_manager
from gsuid_core.ai_core.ai_router import (
    get_ai_session,
)
from gsuid_core.ai_core.classifier import classifier_service
from gsuid_core.ai_core.statistics import statistics_manager
from gsuid_core.ai_core.persona.mood import update_mood, get_mood_description
from gsuid_core.ai_core.memory.config import memory_config
from gsuid_core.ai_core.history_format import format_history_for_agent
from gsuid_core.ai_core.database.models import UserFavorability
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.buildin_tools.subagent import create_subagent
from gsuid_core.ai_core.memory.retrieval.dual_route import dual_route_retrieve

# 历史记录管理器
history_manager = get_history_manager()

# 双层长度防护配置
ABSOLUTE_MAX_LENGTH = 60000  # 绝对上限：超过此长度直接截断，防止子Agent Token爆炸
MAX_SUMMARY_LENGTH = 15000  # 摘要阈值：超过此长度调用子Agent进行智能摘要（调整至8000避免短文本被过度摘要）

# AI并发控制配置
MAX_CONCURRENT_AI_CALLS = 10  # 全局最大并发AI调用数
_ai_semaphore = asyncio.Semaphore(MAX_CONCURRENT_AI_CALLS)  # AI并发信号量

# C4 寒暄门控：回指 / 实体 / 任务引用词，命中则强制触发记忆检索
_FORCE_RETRIEVE_RE = re.compile(
    r"(之前|上次|上回|那个|那次|昨天|前几天|你说过|你不是说|记不记得|还记得|提到过|任务|计划|进度)"
)
# C4 / C3-c：明显情绪词，命中则强制检索（避免错过用户昨日事件背景）
_EMOTION_RETRIEVE_RE = re.compile(r"(难过|崩溃|沉船|破防|开心死|伤心|焦虑|想哭|绝望|委屈|孤独)")
# C3-c 自我情景记忆召回触发词：用户回指 Bot 自己曾经的言行
_SELF_RECALL_RE = re.compile(r"(你之前|你上次|你不是说|你说过|你还记得|你刚才说|你答应)")
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
    if not ai_config.get_config("enable").data:
        logger.debug("🧠 [GsCore][AI] AI服务未启用，跳过处理")
        return

    try:
        from gsuid_core.ai_core.startup import is_ai_core_ready, wait_ai_core_ready

        if not is_ai_core_ready():
            logger.info("🧠 [GsCore][AI] AI Core 正在初始化/迁移，等待初始化完成后再处理本次消息...")
            if not await wait_ai_core_ready(timeout=300.0):
                logger.warning("🧠 [GsCore][AI] AI Core 初始化等待超时，跳过本次消息以避免查询未完成迁移的向量库")
                return
    except Exception as e:
        logger.warning(f"🧠 [GsCore][AI] 检查 AI Core 初始化状态失败，继续降级处理: {e}")

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
            # 步骤 4: 准备用户消息（含好感度注入）
            # ============================================================

            # 查询当前用户好感度（从外部存储，非模型推断）
            favorability: Optional[int] = None
            try:
                # Bot.bot_id 是已声明字段；handle_ai 链路 bot 通常非 None
                bot_id = bot.bot_id if bot is not None else ""
                user_data = await UserFavorability.get_user_favorability(
                    user_id=str(event.user_id),
                    bot_id=bot_id,
                )
                if user_data:
                    favorability = user_data.favorability
            except Exception as e:
                logger.debug(f"🧠 [GsCore][AI] 好感度查询失败，降级为无注入: {e}")

            user_messages = await prepare_content_payload(
                event,
                favorability=favorability,
            )

            # 第二层：智能摘要（在安全范围内对长文本进行摘要）
            # Bug-03修复：摘要时保留上下文头，只替换正文部分
            if len(event.raw_text) > MAX_SUMMARY_LENGTH:
                logger.info(f"🧠 [GsCore][AI] 检测到长文本 ({len(event.raw_text)} 字符)，开始摘要...")

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
                logger.info(f"🧠 [GsCore][AI] 摘要完成，摘要长度: {len(summarized)} 字符")

            # Bug-04修复：时间注入移到摘要之后（无论是否摘要都需要）
            current_time = datetime.now().strftime("%Y-%m-%d %H:%M")
            if isinstance(user_messages, list) and len(user_messages) > 0 and isinstance(user_messages[0], str):
                user_messages[0] += f"\n【当前时间】{current_time}"

            # ============================================================
            # 步骤 5: 记忆上下文（Memory Retrieval）
            # 基于群组/用户ID检索相关记忆，用于个性化响应
            # ============================================================
            memory_context_text = ""
            is_enable_memory: bool = ai_config.get_config("enable_memory").data
            if is_enable_memory and memory_config.enable_retrieval:
                # C4 寒暄门控：纯寒暄（短+闲聊+无实体/情绪/回指）跳过双路检索，
                # 节省向量搜索 + Reranker 开销；其余情况照常检索。
                if not _should_retrieve_memory(query, intent, str(event.user_id)):
                    logger.debug("🧠 [Memory] 命中寒暄门控，跳过双路检索")
                else:
                    try:
                        mem_ctx = await dual_route_retrieve(
                            query=query,
                            group_id=str(event.group_id or event.user_id),
                            user_id=str(event.user_id),
                            top_k=memory_config.retrieval_top_k,
                            enable_system2=memory_config.enable_system2,
                            enable_user_global=memory_config.enable_user_global_memory,
                        )
                        # C4 预算优先级：主人相关记忆优先占用注入预算
                        from gsuid_core.config import core_config

                        masters_set = {str(m) for m in (core_config.get_config("masters") or [])}
                        memory_context_text = mem_ctx.to_prompt_text(
                            max_chars=memory_config.memory_inject_max_chars,
                            priority_speakers=masters_set or None,
                        )
                        logger.debug(f"🧠 [Memory] 检索到记忆上下文 ({len(memory_context_text)} 字符)")
                        # 上报记忆检索统计
                        try:
                            statistics_manager.record_memory_retrieval()
                        except Exception:
                            pass
                    except Exception as e:
                        logger.warning(f"🧠 [Memory] 记忆检索失败: {e}")

            # ============================================================
            # 步骤 6: 历史记录上下文
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

            # ============================================================
            # Fix-06: 当前用户优先的历史窗口过滤
            # 保证当前用户的最近消息一定在窗口内
            # ============================================================
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
                    logger.debug(f"🧠 [GsCore][AI] 已加载 {len(history)} 条历史消息")

            # ============================================================
            # Fix-03: 获取当前情绪状态描述并注入上下文
            # ============================================================
            mood_key = str(event.group_id) if event.group_id else str(event.user_id)
            mood_desc = ""
            if session.persona_name:
                try:
                    mood_desc = await get_mood_description(session.persona_name, mood_key)
                except Exception as e:
                    logger.debug(f"🎭 [Mood] 情绪描述获取失败: {e}")

            # 群组语境注入（群组画像：主要话题 + 词汇映射表）
            # 让 Agent 直接知道"深渊"在本群指什么、某个外号对应哪个角色
            group_context_text = ""
            if event.group_id:
                try:
                    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
                    from gsuid_core.ai_core.memory.group_profile import format_context_injection

                    group_context_text = await format_context_injection(
                        make_scope_key(ScopeType.GROUP, str(event.group_id))
                    )
                except Exception as e:
                    logger.debug(f"🧠 [GsCore][AI] 群组语境注入失败: {e}")

            # ============================================================
            # C3-a/c: 自我认知动态注入
            # 演化层 self_model + 关系 + 能力域，每轮独立拼接到 user message 侧，
            # 绝不写入 persona 目录文件（约束 1：规避热重载滚动销毁会话）。
            # ============================================================
            self_cognition_text = ""
            self_episode_text = ""
            try:
                from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
                from gsuid_core.ai_core.self_cognition import (
                    retrieve_self_episodes,
                    build_self_cognition_context,
                )

                # Bot.bot_id 是已声明字段，直接访问；handle_ai 链路 bot 通常非 None
                bot_id_for_self = bot.bot_id if bot is not None else ""
                # scope_key：让 self_cognition 能用 group_profile 的累计 tag 实时
                # 计算"反复出现的话题"。无 group_id 时退回 user_global scope。
                cognition_scope = make_scope_key(
                    ScopeType.GROUP if event.group_id else ScopeType.USER_GLOBAL,
                    str(event.group_id) if event.group_id else str(event.user_id),
                )
                self_cognition_text = await build_self_cognition_context(
                    bot_id=bot_id_for_self,
                    user_id=str(event.user_id),
                    favorability=favorability,
                    scope_key=cognition_scope,
                )
                # C3-c: 用户回指 Bot 自己曾经的言行时，召回自我情景记忆
                if _SELF_RECALL_RE.search(query):
                    self_episode_text = await retrieve_self_episodes(bot_id_for_self)
            except Exception as e:
                logger.debug(f"🪞 [SelfCognition] 自我认知注入失败: {e}")

            # ============================================================
            # C5: 长任务进度动态注入
            # 注入当前用户的活跃长任务摘要（仅短序号、无 UUID），
            # 让用户可追问"那个任务怎么样了"，Agent 也不对自己在跑的长任务失明。
            # ============================================================
            task_context_text = ""
            try:
                from gsuid_core.ai_core.planning.context import build_task_context

                task_context_text = await build_task_context(str(event.user_id))
            except Exception as e:
                logger.debug(f"📋 [Planning] 长任务上下文注入失败: {e}")

            # 组装完整上下文
            context_parts = []
            if rag_context:
                context_parts.append(rag_context)
            if group_context_text:
                context_parts.append(group_context_text)
            # Prompt-2.5: 用括号包裹情绪状态，暗示这是内心状态而非对话指令
            if mood_desc:
                context_parts.append(f"（{mood_desc}。）")
            if self_cognition_text:
                context_parts.append(self_cognition_text)
            # 逐轮人格口吻锚点（治理长会话的人格漂移）：人格只在会话创建时固化进
            # system_prompt，越聊越靠后、注意力越稀释。此处每轮补一行紧凑口吻自述。
            try:
                from gsuid_core.ai_core.persona import get_voice_anchor

                voice_anchor = get_voice_anchor(session.persona_name) if session.persona_name else ""
                if voice_anchor:
                    context_parts.append(f"（口吻锚点：{voice_anchor}）")
            except Exception as e:
                logger.debug(f"🧠 [GsCore][AI] 人格口吻锚点注入失败: {e}")
            if self_episode_text:
                context_parts.append(self_episode_text)
            if task_context_text:
                context_parts.append(task_context_text)
            if memory_context_text:
                context_parts.append(f"【长期记忆】\n{memory_context_text}")

            full_context = "\n\n".join(context_parts)

            # ============================================================
            # 步骤 7: 调用 Agent 生成回复
            # Agent 会根据对话内容自主决定是否调用 search_knowledge 工具
            # ============================================================
            chat_result = await session.run(
                user_message=user_messages,
                bot=bot,
                ev=event,
                rag_context=full_context,
                return_mode="by_bot",  # 由 Agent 决定何时通过 bot 发送回复
            )

            # 步骤 8: 发送回复
            if chat_result:
                # 拦截沉默信号
                result_text = chat_result if isinstance(chat_result, str) else str(chat_result)
                if result_text.strip() in SILENCE_MARKERS:
                    logger.info("🧠 [GsCore][AI] 角色选择沉默，不发送回复")
                    # 情绪仍然正常更新，只是不发消息
                else:
                    await send_chat_result(bot, chat_result, ev=event)
                    logger.info(f"🧠 [GsCore][AI] 回复已发送 (模式: {intent})")

            # ============================================================
            # 步骤 9: 更新 Persona 情绪状态（异步，不阻塞主流程）
            # 根据用户消息内容推断情绪事件类型
            # 群聊使用 group_id，私聊使用 user_id 作为情绪隔离 key
            # ============================================================
            if session.persona_name:
                mood_key = str(event.group_id) if event.group_id else str(event.user_id)
                from gsuid_core.ai_core.utils import _is_master_user

                mood_task = asyncio.create_task(
                    _update_persona_mood(
                        persona_name=session.persona_name,
                        group_id=mood_key,
                        user_message=query,
                        is_master=_is_master_user(str(event.user_id)),
                    )
                )
                # 安全获取底层 _Bot 实例，兼容 Bot 和 MockBot
                # 注意：先判断 Bot（更具体的子类），再判断 _Bot（更宽泛的父类），
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
                        "🧠 [GsCore][AI] 无法获取 _Bot 实例，mood_task 未被注册到 bg_tasks，可能导致 Task 游离"
                    )

        except Exception as e:
            logger.exception(f"🧠 [GsCore][AI] 聊天异常: {e}")


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
        logger.debug(f"🎭 [Mood] 情绪更新失败: {e}")
