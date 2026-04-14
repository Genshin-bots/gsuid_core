"""Subagent 工具模块

提供创建子Agent的能力，允许AI搜索合适的System Prompt
并生成子Agent来完成特定任务，结果返回给主Agent。
"""

from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.gs_agent import create_agent
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.rag.tools import search_tools
from gsuid_core.ai_core.system_prompt import get_best_match

# 子Agent最大迭代次数上限，防止死循环
_SUBAGENT_MAX_ITERATIONS = 3


@ai_tools(category="self")
async def create_subagent(
    ctx: RunContext[ToolContext],
    task: str,
    tags: Optional[str] = None,
    max_tokens: int = 1800,
    max_iterations: int = _SUBAGENT_MAX_ITERATIONS,
) -> str:
    """
    创建子Agent完成特定任务

    根据任务描述搜索最匹配的System Prompt，
    创建一个临时子Agent来完成任务，结果返回给主Agent。

    这对于复杂任务分解、多角度分析、角色扮演等场景特别有用。
    子Agent拥有独立的上下文，可以专注于特定任务。

    Args:
        ctx: 工具执行上下文
        task: 要完成的任务描述，请详细描述任务需求
        tags: 可选，限定System Prompt的标签，如"代码专家"、"角色扮演"等
        max_tokens: 子Agent最大输出token数，默认1800
        max_iterations: 子Agent最大迭代次数（工具调用轮数），默认3次，防止死循环

    Returns:
        子Agent的执行结果

    Example:
        >>> result = await create_subagent(ctx, "写一个Python快速排序函数", tags=["代码"])
        >>> result = await create_subagent(ctx, "以角色的语气回复: 今天天气真好", tags=["角色扮演"])
    """
    # 解析tags
    tag_list = None
    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    # 搜索最匹配的System Prompt
    matched_prompt = await get_best_match(
        query=task,
        tags=tag_list,
    )

    if not matched_prompt:
        return "⚠️ 没有找到匹配的系统提示词，请尝试不同的任务描述或标签。"

    # 搜索工具
    tools = await search_tools(query=task, limit=5, non_category="self")

    logger.info(f"🧠 [Subagent] 匹配到System Prompt: {matched_prompt.get('title', 'unknown')}")

    # 构建系统提示词
    system_prompt = matched_prompt.get("content", "")
    if not system_prompt:
        return "⚠️ 匹配的系统提示词内容为空。"

    # 创建子Agent，传递 max_iterations 限制防止死循环
    agent = create_agent(
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        max_iterations=max_iterations,
    )

    try:
        # 运行子Agent
        logger.info(f"🧠 [Subagent] 开始执行子Agent任务: {task[:50]}...")

        result = await agent.run(
            user_message=task,
            bot=ctx.deps.bot,
            ev=ctx.deps.ev,
            tools=tools,
        )

        logger.info(f"🧠 [Subagent] 子Agent执行完成，结果长度: {len(result)}")
        return result

    except Exception as e:
        logger.error(f"❌ [Subagent] 子Agent执行失败: {e}")
        return f"⚠️ 子Agent执行出错: {str(e)}"
