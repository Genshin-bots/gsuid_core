"""
PydanticAI Agent 核心模块
基于 pydantic_ai 实现的轻量级 Agent
"""

import time
import asyncio
from typing import Any, Set, Dict, List, Union, Literal, TypeVar, Optional, Sequence, overload

import httpx
from pydantic_ai import Agent
from pydantic_graph import End
from pydantic_ai.agent import CallToolsNode, ModelRequestNode
from pydantic_ai.usage import UsageLimits
from pydantic_ai.messages import (
    TextPart,
    UserContent,
    ModelMessage,
    ThinkingPart,
    ToolCallPart,
    ModelResponse,
    ToolReturnPart,
)
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModel

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.utils import send_chat_result
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.skills import skills_toolset
from gsuid_core.ai_core.rag.tools import ToolList, search_tools, get_main_agent_tools
from gsuid_core.ai_core.configs.models import get_model_for_task
from gsuid_core.ai_core.persona.prompts import CHARACTER_BUILDING_TEMPLATE
from gsuid_core.ai_core.configs.ai_config import ai_config

_T = TypeVar("_T")


def _truncate_history_with_tool_safety(
    history: List[ModelMessage],
    max_history: int,
) -> List[ModelMessage]:
    """
    安全截断 history，确保 ToolCallPart 和 ToolReturnPart 保持配对。

    问题：如果简单地从末尾截断 history，可能导致 ToolReturnPart 被保留
    但其对应的 ToolCallPart 被丢弃，从而在下一轮请求时出现
    "tool result's tool id not found" 错误。

    解决策略：
    1. 从头扫描所有消息，收集所有 tool_call_id 及其出现位置
    2. ToolCallPart 出现在 ModelResponse 中，ToolReturnPart 出现在 ModelRequest 中
    3. 追踪每个 tool_call_id 的 call 和 return 是否配对
    4. 如果截断点落在未配对的范围内，则扩展截断点
    5. 确保所有保留的 ToolReturnPart 都有对应的 ToolCallPart

    Args:
        history: 原始消息历史
        max_history: 最大保留消息数

    Returns:
        截断后的安全消息历史
    """
    if len(history) <= max_history:
        return history

    from pydantic_ai.messages import ModelRequest

    # 第一步：扫描所有消息，收集 tool_call_id 的位置和类型
    # call_ids: 记录哪些 tool_call_id 有 ToolCallPart（出现在 ModelResponse）
    # return_ids: 记录哪些 tool_call_id 有 ToolReturnPart（出现在 ModelRequest）
    # call_positions: 每个 tool_call_id 的 ToolCallPart 所在的消息索引
    # return_positions: 每个 tool_call_id 的 ToolReturnPart 所在的消息索引
    call_ids: Set[str] = set()
    return_ids: Set[str] = set()
    call_positions: Dict[str, List[int]] = {}
    return_positions: Dict[str, List[int]] = {}

    for idx, msg in enumerate(history):
        if isinstance(msg, ModelResponse):
            for part in msg.parts:
                if isinstance(part, ToolCallPart):
                    call_ids.add(part.tool_call_id)
                    if part.tool_call_id not in call_positions:
                        call_positions[part.tool_call_id] = []
                    call_positions[part.tool_call_id].append(idx)
        elif isinstance(msg, ModelRequest):
            for part in msg.parts:
                if isinstance(part, ToolReturnPart):
                    return_ids.add(part.tool_call_id)
                    if part.tool_call_id not in return_positions:
                        return_positions[part.tool_call_id] = []
                    return_positions[part.tool_call_id].append(idx)

    # 第二步：找出孤立的 tool return（有 return 但没有 call）
    orphaned_returns = return_ids - call_ids

    if not orphaned_returns:
        # 没有孤立的 tool return，可以安全地从末尾截断
        truncated = history[-max_history:]
        logger.debug(f"🧠 [GsCoreAIAgent] 安全截断 history: {len(history)} -> {len(truncated)} (无孤立 tool return)")
        return truncated

    # 第三步：找到所有包含孤立 tool return 的消息位置
    orphaned_msg_indices: Set[int] = set()
    for tool_call_id in orphaned_returns:
        if tool_call_id in return_positions:
            orphaned_msg_indices.update(return_positions[tool_call_id])

    # 第四步：确定截断点
    truncate_index = len(history) - max_history

    # 检查是否有孤立的 msg indices 在截断点之后
    orphaned_in_tail = [i for i in orphaned_msg_indices if i >= truncate_index]

    if orphaned_in_tail:
        # 需要扩展截断范围，确保孤立的 tool return 被包含或连同其 call 一起被保留
        min_orphaned_idx = min(orphaned_in_tail)
        # 扩展截断范围，留出更多空间确保配对完整
        new_truncate_index = max(0, min_orphaned_idx - 5)
        truncated = history[new_truncate_index:]
        logger.warning(
            f"🧠 [GsCoreAIAgent] 检测到 {len(orphaned_returns)} 个孤立 tool return，"
            f"扩展截断范围: {len(history)} -> {len(truncated)} (从索引 {new_truncate_index} 开始)"
        )
        return truncated

    truncated = history[-max_history:]
    logger.debug(f"🧠 [GsCoreAIAgent] 安全截断 history: {len(history)} -> {len(truncated)}")
    return truncated


class GsCoreAIAgent:
    """
    基于 PydanticAI 的 Agent 封装类

    Attributes:
        model_name: 模型名称
        api_key: API 密钥
        base_url: API 基础 URL
        max_tokens: 最大输出 token 数
        system_prompt: 系统提示词
    """

    def __init__(
        self,
        openai_chat_model: Optional[OpenAIChatModel] = None,
        system_prompt: Optional[str] = None,
        max_tokens: int = 20000,
        max_iterations: Optional[int] = None,
        persona_name: Optional[str] = None,
        max_history: int = 20,
        create_by: str = "LLM",
        task_level: Literal["high", "low"] = "high",
    ):
        self.history: List[ModelMessage] = []
        self.max_history = max_history
        self.system_prompt = system_prompt
        self.persona_name = persona_name  # 用于热重载检查
        # 用于串行执行 run 方法的锁
        self._run_lock = asyncio.Lock()
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations  # 自定义迭代次数限制，None时使用配置默认值
        self.task_level = task_level  # 任务级别，用于选择对应的模型配置

        self.create_by = create_by

        self.model = openai_chat_model
        if self.model is None:
            self.model = get_model_for_task(task_level)

    def extract_history(self):
        if self.max_history <= 0:
            self.history = []

        if len(self.history) > self.max_history:
            self.history = _truncate_history_with_tool_safety(
                self.history,
                self.max_history,
            )
            logger.debug(f"🧠 [GsCoreAIAgent] 历史记录已截断至 {len(self.history)} 条")

    @overload
    async def _execute_run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        must_return: bool = False,
        output_type: None = None,
    ) -> str: ...

    @overload
    async def _execute_run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        must_return: bool = False,
        output_type: type[_T] = ...,
    ) -> _T: ...

    async def _execute_run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        must_return: bool = False,
        output_type: Optional[type] = None,
    ) -> Union[str, Any]:
        """
        实际执行 Agent 运行的内部方法

        Args:
            output_type: 当指定为某个 Pydantic 模型类时，利用 pydantic_ai 的
                output_type 特性，要求模型必须返回符合该模型结构的 JSON。
                此时返回值为该 Pydantic 模型实例而非字符串。
        """
        from gsuid_core.ai_core.statistics import statistics_manager

        # 使用自定义迭代次数限制（如果有），否则使用配置默认值
        if self.max_iterations is not None:
            limits = UsageLimits(request_limit=self.max_iterations)
        else:
            multi_agent_lenth: int = ai_config.get_config("multi_agent_lenth").data
            limits = UsageLimits(request_limit=multi_agent_lenth)

        # 记录开始时间用于延迟统计
        start_time = time.time()

        logger.info("🧠 [GsCoreAIAgent] ====== Agent 运行开始 ======")
        context = ToolContext(bot=bot, ev=ev)

        final_user_message = f"【用户发言】\n{user_message}"

        if rag_context:
            if isinstance(final_user_message, str):
                final_user_message = f"{final_user_message}\n\n{rag_context}"
            elif isinstance(final_user_message, list):
                final_user_message = list(final_user_message)
                final_user_message.append(f"\n\n{rag_context}")
            logger.info("🧠[GsCoreAIAgent] 已添加 RAG 上下文")

        logger.trace(f"🧠[GsCoreAIAgent] 用户消息: {final_user_message}")

        if tools is None:
            tools = []

        if self.create_by in ["SubAgent", "Chat", "Agent", "AutoPlanner"]:
            if not tools:
                tools = get_main_agent_tools()
                qy = ""
                if isinstance(user_message, str):
                    qy = user_message
                elif ev is not None:
                    qy = ev.raw_text

                if qy:
                    logger.debug(f"🧠 [GsCoreAIAgent] 尝试搜索工具: {qy}")
                    tools += await search_tools(
                        query=qy,
                        limit=3,
                        non_category=["self", "buildin"],
                    )
                logger.debug(f"🧠 [GsCoreAIAgent] 主Agent工具数量: {len(tools)}")
            else:
                logger.debug(f"🧠 [GsCoreAIAgent] 传入Tools列表: {len(tools)}，已传入参数")
        else:
            logger.debug("🧠 [GsCoreAIAgent] 不搜索工具")

        logger.debug(f"🧠 [GsCoreAIAgent] 工具列表: {[tool.name for tool in tools]}")

        tools = list({obj.name: obj for obj in tools}.values())

        # 当 return_model 指定时，使用 output_type 让 pydantic_ai 强制结构化输出
        # output_type 默认为 str（返回文本），指定 Pydantic 模型时强制返回结构化 JSON
        _agent = Agent(
            model=self.model,
            deps_type=ToolContext,
            system_prompt=self.system_prompt or "你是一个智能助手, 简短的一句话回答问题即可。",
            model_settings={"max_tokens": self.max_tokens},
            tools=tools,
            toolsets=[skills_toolset],
            retries=3,
            output_type=output_type or str,
        )

        # 截断历史记录，避免无限制增长
        self.extract_history()

        try:
            logger.info("🧠 [GsCoreAIAgent] 开始执行 _agent.iter()...")
            logger.info(f"🧠 [GsCoreAIAgent] 当前 history: {len(self.history)}")

            now_text = ""
            async with _agent.iter(
                final_user_message,
                deps=context,  # type: ignore[arg-type]
                message_history=self.history,
                usage_limits=limits,
            ) as agent_run:
                # 遍历每一步 Node
                async for node in agent_run:
                    # 1. 发起大模型请求前的处理
                    if isinstance(node, ModelRequestNode):
                        logger.debug("🧠 [GsCoreAIAgent] ⚡ 触发节点: ModelRequestNode")

                        for part in node.request.parts:
                            if isinstance(part, ToolReturnPart):
                                # 返回的可能是对象也可能是字符串，这里为了打印转成 str
                                tool_result_str = str(part.content)
                                logger.debug(
                                    f"[✅ 工具执行完毕]: 工具名称='{part.tool_name}', 结果给到Agent={tool_result_str}"
                                )

                                # 你也可以在这里发送安抚话语
                                if context.bot and context.ev:
                                    done_msg = f"✅ 「{part.tool_name}」操作执行完毕！"
                                    if bot:
                                        await bot.send(done_msg)

                        logger.debug("🧠  ▶ [发起请求]: 正在等待大模型思考...")

                    # 2. 获取到大模型响应，准备调用工具或者输出文本
                    # 这里使用了 isinstance，Pyright 就能明确知道此时 node 是 CallToolsNode，拥有 model_response 属性
                    elif isinstance(node, CallToolsNode):
                        logger.debug("🧠 [GsCoreAIAgent] ⚡ 触发节点: CallToolsNode")

                        # 遍历大模型返回的具体片段 (Parts)
                        for part in node.model_response.parts:
                            # ✨ 拦截到模型即将调用工具！
                            if isinstance(part, ToolCallPart):
                                logger.debug(f"[🔧 大模型请求调用工具]: 工具名称='{part.tool_name}', 参数={part.args}")

                                # 利用传入的 Bot 实时发送安抚话语
                                if context.bot and context.ev:
                                    waiting_msg = f"⏳ 正在为你执行「{part.tool_name}」操作..."
                                    if bot:
                                        await bot.send(waiting_msg)

                            # 大模型直接输出文本
                            elif isinstance(part, TextPart):
                                _text = part.content.strip()
                                logger.debug(f"🧠 [大模型文本]: {_text}")
                                if bot and _text and not must_return:
                                    await send_chat_result(bot, _text)
                                    now_text = _text

                            elif isinstance(part, ThinkingPart):
                                _thinking = part.content.strip()
                                logger.trace(f"🧠 [大模型思考]: {_thinking}")
                                if bot and _thinking:
                                    pass

                    # 3. 运行结束节点
                    elif isinstance(node, End):
                        logger.debug("🧠 [GsCoreAIAgent] ⚡ 触发节点: End")
                        logger.debug("  ✅ [运行结束]: 最终结果生成完毕")

            # 遍历完成后，直接从 agent_run 中获取最终结果
            result = agent_run.result
            if result:
                logger.info("🧠 [GsCoreAIAgent] _agent.iter() 执行成功!")

                self.history.extend(result.new_messages())

                # 记录 Token 使用量和延迟统计
                try:
                    # 记录响应延迟
                    latency = time.time() - start_time
                    statistics_manager.record_latency(latency=latency)

                    try:
                        usage_obj = result.usage()
                        input_tokens: int = usage_obj.input_tokens
                        output_tokens: int = usage_obj.output_tokens
                        logger.info(f"📊 [GsCoreAIAgent] Token消耗: input={input_tokens}, output={output_tokens}")
                        if input_tokens > 0 or output_tokens > 0:
                            statistics_manager.record_token_usage(
                                model_name=self.model.model_name if self.model else "unknown",
                                chat_type=self.create_by,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                            )
                            statistics_manager.record_token_usage
                    except AttributeError as e:
                        # result 没有 usage 属性（如 pydantic_graph End 节点返回的结果）
                        logger.info(f"📊 [GsCoreAIAgent] result.usage 访问失败: {e}")
                        pass
                except Exception as e:
                    logger.warning(f"📊 [GsCoreAIAgent] 记录统计失败: {e}")

                # 当 return_model 指定时，直接返回 Pydantic 模型实例
                if output_type is not None:
                    return result.output
                # 始终返回字符串类型
                result_msg = str(result.output).strip()
                if now_text.strip() == result_msg.strip() and not must_return:
                    return ""
                return str(result.output).strip()

            # result 为空时的默认返回值
            return "Agent 执行完成，但未返回有效结果"

        except UsageLimitExceeded:
            # 达到限制后的处理逻辑
            logger.warning(f"🧠 [PydanticAI] Agent 达到最高思考轮数限制 {limits.request_limit}")
            statistics_manager.record_error(error_type="usage_limit")

            # 安抚用户
            if bot:
                await bot.send("⏳ 思考链过长，正在根据已有线索为你整理最终结论...")

            # ✨ 【关键点2】发起“强制总结”请求
            try:
                # 追加一条系统强制指令，严禁再次调用工具
                fallback_prompt = (
                    "（系统强制指令）：你先前的思考和工具调用已达到最大轮数限制被强制中断。"
                    "请立即停止使用任何工具，并仅根据上述已获取的所有信息，根据用户的问题给出一个总结性的最终回答或结论。"
                )

                # 使用上一次崩溃前保存的上下文，再次请求大模型
                fallback_result = await _agent.run(
                    fallback_prompt,
                    deps=context,
                    message_history=self.history,
                    usage_limits=UsageLimits(request_limit=1),
                )

                # 获取强制总结的文本并发送
                now_text = fallback_result.output
                if bot:
                    await send_chat_result(bot, fallback_result.output)
                return ""

            except Exception as e:
                # 如果强制总结还报错（比如最后一次它还死活要调工具导致再次超限），再使用保底话术
                logger.error(f"🧠 [PydanticAI] 强制总结失败: {e}")
                error_msg = "⚠️ 这个问题太复杂了，目前获取到的信息不足以给出准确答案。"
                "可以尝试提高思维链长度，或者尝试其他方法解决问题。"
                return error_msg

        except httpx.TimeoutException as e:
            # HTTP 请求超时
            logger.warning(f"🧠 [PydanticAI] Agent 运行异常: 请求超时 {e}")
            statistics_manager.record_error(error_type="timeout")
            return "执行出错: 请求超时"

        except httpx.HTTPError as e:
            # 其他 HTTP 错误（网络相关）
            error_str = str(e).lower()
            if "rate" in error_str or "429" in error_str or "limit" in error_str:
                logger.warning(f"🧠 [PydanticAI] Agent 运行异常: Rate Limit {e}")
                statistics_manager.record_error(error_type="rate_limit")
            else:
                logger.warning(f"🧠 [PydanticAI] Agent 运行异常: 网络错误 {e}")
                statistics_manager.record_error(error_type="network_error")
            return f"执行出错: {str(e)}"

        except Exception as e:
            logger.error(f"🧠 [PydanticAI] Agent 运行异常: {e}")
            logger.exception("🧠 [PydanticAI] 异常详情:")
            if "529" in str(e):
                statistics_manager.record_error(error_type="api_529_error")
            else:
                statistics_manager.record_error(error_type="agent_error")
            return f"执行出错: {str(e)}"

    @overload
    async def run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        must_return: bool = False,
        output_type: None = None,
    ) -> str: ...

    @overload
    async def run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        must_return: bool = False,
        output_type: type[_T] = ...,
    ) -> _T: ...

    async def run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
        tools: Optional[ToolList] = None,
        must_return: bool = False,
        output_type: Optional[type] = None,
    ) -> Union[str, Any]:
        """
        运行 Agent 并返回结果

        此方法使用锁机制确保同一时间只有一个请求在执行，
        其他请求会挂起等待，执行时自动继承历史记录

        Args:
            output_type: 当指定为某个 Pydantic 模型类时，利用 pydantic_ai 的
                output_type 特性，要求模型必须返回符合该模型结构的 JSON。
                此时返回值为该 Pydantic 模型实例而非字符串。

        Returns:
            Agent 执行结果。默认返回 str，当 output_type 指定时返回对应模型实例
        """
        async with self._run_lock:
            logger.info("🧠 [GsCoreAIAgent] 获取到执行锁，开始执行...")
            result = await self._execute_run(
                user_message=user_message,
                bot=bot,
                ev=ev,
                rag_context=rag_context,
                tools=tools,
                must_return=must_return,
                output_type=output_type,
            )
            logger.info("🧠 [GsCoreAIAgent] 执行完成，释放锁")
            return result


# 工厂函数
def create_agent(
    system_prompt: Optional[str] = None,
    max_tokens: int = 20000,
    max_iterations: Optional[int] = None,
    persona_name: Optional[str] = None,
    create_by: str = "LLM",
    max_history: int = 20,
    task_level: Literal["high", "low"] = "high",
) -> GsCoreAIAgent:
    """
    创建 PydanticAI Agent 实例

    Args:
        model_name: 模型名称
        system_prompt: 系统提示词
        max_tokens: 最大输出 token 数
        max_iterations: 最大迭代次数限制，None 时使用配置默认值
        persona_name: Persona 名称（用于热重载检测）
        task_level: 任务级别，"high"表示高级任务，"low"表示低级任务

    Returns:
        PydanticAIAgent 实例

    Example:
        agent = create_agent(
            system_prompt='你是一个智能助手。',
        )
    """
    return GsCoreAIAgent(
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        max_iterations=max_iterations,
        persona_name=persona_name,
        create_by=create_by,
        max_history=max_history,
        task_level=task_level,
    )


async def build_new_persona(query: str) -> str:
    """
    构建新的角色提示词

    使用角色构建模板和用户查询，生成新的角色提示词。

    Args:
        query: 用户查询，描述新角色的特征和能力

    Returns:
        新角色的提示词字符串
    """
    agent = create_agent(
        system_prompt=CHARACTER_BUILDING_TEMPLATE,
        create_by="BuildPersona",
        task_level="high",
    )
    response = await agent.run(query)
    return response.strip()
