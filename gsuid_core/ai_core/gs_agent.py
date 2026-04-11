"""
PydanticAI Agent 核心模块
基于 pydantic_ai 实现的轻量级 Agent
"""

import time
import asyncio
from typing import TYPE_CHECKING, Any, List, Union, Optional, Sequence

from pydantic_ai import Agent
from pydantic_graph import End
from pydantic_ai.usage import UsageLimits
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

if TYPE_CHECKING:
    from pydantic_ai.tools import Tool

from pydantic_ai.agent import CallToolsNode, ModelRequestNode
from pydantic_ai.messages import TextPart, UserContent, ModelMessage, ThinkingPart, ToolCallPart

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.skills import skills_toolset
from gsuid_core.ai_core.register import _TOOL_REGISTRY, _BUILDIN_TOOLS_REGISTRY
from gsuid_core.ai_core.ai_config import ai_config, openai_config
from gsuid_core.ai_core.rag.tools import search_tools
from gsuid_core.ai_core.statistics import statistics_manager

if TYPE_CHECKING:
    ToolList = List["Tool[ToolContext]"]
else:
    ToolList = List[Any]


def get_tools(tool_names: Optional[List[str]] = None) -> ToolList:
    """根据工具名称列表获取工具对象

    Args:
        tool_names: 工具名称列表，如果为None则返回空列表

    Returns:
        ToolList: 工具对象列表，用于传递给 pydantic_ai Agent
    """
    if not tool_names:
        return []

    plugin_tools = [_TOOL_REGISTRY[name].tool for name in tool_names if name in _TOOL_REGISTRY]
    buildin_tools = [_BUILDIN_TOOLS_REGISTRY[n].tool for n in _BUILDIN_TOOLS_REGISTRY]
    return buildin_tools + plugin_tools


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
        model_name: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_tokens: int = 1800,
        persona_name: Optional[str] = None,
    ):
        self.history: List[ModelMessage] = []
        self.system_prompt = system_prompt
        self.persona_name = persona_name  # 用于热重载检查
        # 用于串行执行 run 方法的锁
        self._run_lock = asyncio.Lock()

        if model_name:
            self.model_name = model_name
        else:
            self.model_name = openai_config.get_config("model_name").data

        if api_key:
            self.api_key = api_key
        else:
            self.api_key = openai_config.get_config("api_key").data[0]

        if base_url:
            self.base_url = base_url
        else:
            self.base_url = openai_config.get_config("base_url").data

        self.max_tokens = max_tokens

    async def _execute_run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
    ) -> str:
        """
        实际执行 Agent 运行的内部方法
        """
        multi_agent_lenth: int = ai_config.get_config("multi_agent_lenth").data
        limits = UsageLimits(request_limit=multi_agent_lenth)

        # 记录开始时间用于延迟统计
        start_time = time.time()

        logger.info("🧠 [GsCoreAIAgent] ====== Agent 运行开始 ======")
        context = ToolContext(bot=bot, ev=ev)

        final_user_message = user_message
        if rag_context:
            if isinstance(final_user_message, str):
                final_user_message = f"{final_user_message}\n\n{rag_context}"
            elif isinstance(final_user_message, list):
                final_user_message = list(final_user_message)
                final_user_message.append(f"\n\n{rag_context}")
            logger.info("🧠[GsCoreAIAgent] 2. 已添加 RAG 上下文")

        tool_names = await search_tools(str(user_message), limit=7)
        tools = get_tools(tool_names)

        logger.debug(f"🧠 [GsCoreAIAgent] 本轮命令获取工具数量: {len(tools)}")
        logger.debug(f"🧠 [GsCoreAIAgent] 本轮命令获取工具: {tool_names}")

        now_text = ""

        _agent: Agent[ToolContext, str] = Agent(
            model=OpenAIChatModel(
                model_name=self.model_name,
                provider=OpenAIProvider(api_key=self.api_key, base_url=self.base_url),
            ),
            deps_type=ToolContext,
            system_prompt=self.system_prompt or "你是一个智能助手, 简短的一句话回答问题即可。",
            model_settings={"max_tokens": self.max_tokens},
            tools=tools,
            toolsets=[skills_toolset],
        )

        try:
            logger.info("🧠 [GsCoreAIAgent] 5. 开始执行 _agent.iter()...")

            async with _agent.iter(
                final_user_message,
                deps=context,
                message_history=self.history,
                usage_limits=limits,
            ) as agent_run:
                # 遍历每一步 Node
                async for node in agent_run:
                    # 1. 发起大模型请求前的处理
                    if isinstance(node, ModelRequestNode):
                        logger.debug("🧠 [GsCoreAIAgent] ⚡ 触发节点: ModelRequestNode")
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
                                if bot and _text:
                                    await bot.send(_text)
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
                logger.info("🧠[GsCoreAIAgent] 6. _agent.iter() 执行成功")

                self.history.extend(result.new_messages())

                # 截断历史记录，避免无限制增长
                max_history = 50
                if len(self.history) > max_history:
                    self.history = self.history[-max_history:]
                    logger.debug(f"🧠 [GsCoreAIAgent] 历史记录已截断至 {max_history} 条")

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
                                model_name=self.model_name,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                            )
                    except AttributeError as e:
                        # result 没有 usage 属性（如 pydantic_graph End 节点返回的结果）
                        logger.info(f"📊 [GsCoreAIAgent] result.usage 访问失败: {e}")
                        pass
                except Exception as e:
                    logger.warning(f"📊 [GsCoreAIAgent] 记录统计失败: {e}")

                # 始终返回字符串类型
                result_msg = str(result.output).strip()
                if now_text.strip() == result_msg.strip():
                    return ""
                return str(result.output).strip()

            # result 为空时的默认返回值
            return "Agent 执行完成，但未返回有效结果"

        except UsageLimitExceeded:
            # 达到限制后的处理逻辑
            error_msg = "⚠️ 这个问题太复杂了!"
            logger.warning(f"🧠 [PydanticAI] Agent 运行异常: 达到最高思考轮数限制 {limits.request_limit}")
            statistics_manager.record_error(error_type="usage_limit")
            return error_msg

        except Exception as e:
            logger.error(f"🧠 [PydanticAI] Agent 运行异常: {e}")
            logger.exception("🧠 [PydanticAI] 异常详情:")
            statistics_manager.record_error(error_type="agent_error")
            return f"执行出错: {str(e)}"

    async def run(
        self,
        user_message: Union[str, Sequence[UserContent]],
        bot: Optional[Bot] = None,
        ev: Optional[Event] = None,
        rag_context: Optional[str] = None,
    ) -> str:
        """
        运行 Agent 并返回结果

        此方法使用锁机制确保同一时间只有一个请求在执行，
        其他请求会挂起等待，执行时自动继承历史记录

        Returns:
            Agent 执行结果，可能是 str 或其他类型（取决于 Agent 配置）
        """
        async with self._run_lock:
            logger.info("🧠 [GsCoreAIAgent] 获取到执行锁，开始执行...")
            result = await self._execute_run(
                user_message=user_message,
                bot=bot,
                ev=ev,
                rag_context=rag_context,
            )
            logger.info("🧠 [GsCoreAIAgent] 执行完成，释放锁")
            return result


# 工厂函数
def create_agent(
    model_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    persona_name: Optional[str] = None,
) -> GsCoreAIAgent:
    """
    创建 PydanticAI Agent 实例

    Args:
        model_name: 模型名称
        system_prompt: 系统提示词
        persona_name: Persona 名称（用于热重载检测）

    Returns:
        PydanticAIAgent 实例

    Example:
        agent = create_pydantic_agent(
            system_prompt='你是一个智能助手。',
        )
    """
    return GsCoreAIAgent(
        model_name=model_name,
        system_prompt=system_prompt,
        persona_name=persona_name,
    )
