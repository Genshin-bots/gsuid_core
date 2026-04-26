"""Subagent 工具模块

提供创建子Agent的能力，允许AI搜索合适的System Prompt
并生成子Agent来完成特定任务，结果返回给主Agent。
"""

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.gs_agent import create_agent
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.rag.tools import search_tools

# 子Agent最大迭代次数上限，防止死循环
_SUBAGENT_MAX_ITERATIONS = 3


@ai_tools(category="self")
async def create_subagent(
    ctx: RunContext[ToolContext],
    task: str,
    max_tokens: int = 10000,
    max_iterations: int = 15,  # 规划+执行通常需要较多轮次
) -> str:
    """
    处理复杂任务的终极工具。
    当用户的问题需要：多步拆解、深度调研、长时间执行、收集大量资料时调用此工具。
    它会自动创建一个具备“规划 -> 逐步执行 -> 校验 -> 总结”能力的自主Agent。

    Args:
        ctx: 工具执行上下文
        task: 需要完成的复杂任务描述。请把用户的原始意图清晰地转述在这里。
    """
    logger.info(f"🧠 [Subagent] 启动通用规划执行Agent，任务: {task[:50]}...")

    # 搜索工具
    tools = await search_tools(query=task, limit=8, non_category="self")
    logger.debug(f"🧠 [Subagent] 工具列表: {[tool.name for tool in tools]}")

    # ✨ 核心：内置一个极其强大的 Plan-and-Solve System Prompt
    system_prompt = """
    你是一个极其聪明且自主的“规划与执行专家（Plan-and-Solve Agent）”。
    你不会一次性瞎猜答案，而是严格遵循以下工作流来解决给定的复杂任务：

    【工作流】
    1. 📝 规划阶段 (Plan)：
       - 分析任务，在你的回答中首先输出一个清晰的 `<TODO_LIST>`。
       - 把复杂任务拆解成 2~5 个具体的、可执行的小步骤。
    2. 🛠️ 执行阶段 (Execute)：
       - 根据你的 TODO List，依次调用你拥有的工具去完成每一步。
       - 每执行完一步，在心里打个勾，并根据工具返回的结果决定下一步。
    3. 🧐 校验阶段 (Verify)：
       - 检查你收集到的信息是否已经足够回答用户的原始任务？如果有遗漏，继续调用工具补充。
    4. 🏁 总结阶段 (Final Output)：
       - 任务全部完成后，整理所有获得的信息，给出一个极其高质量、详尽的最终结论或成果。

    【注意】：必须确保最终输出的内容是直接针对任务的最终结果，不要只输出规划过程。
    【注意】：**优先使用已有的专业工具(AI Tools)**
        如果没有合适的工具则调用skill列表,
        如果skill列表也没有合适的工具**才考虑**调用web_search工具去实际搜索。
    """

    agent = create_agent(
        system_prompt=system_prompt,
        max_tokens=max_tokens,
        max_iterations=max_iterations,
        create_by="AutoPlanner",
        task_level="high",
    )

    try:
        # 直接把任务扔给它，它会被 system_prompt 逼着去先列 TODO list
        result = await agent.run(
            user_message=f"【当前任务】\n{task}\n\n请立即开始你的规划与执行！",
            tools=tools,
        )

        return f"【子Agent规划并执行完毕，交付结果如下】\n\n{result}"

    except Exception as e:
        logger.error(f"❌[Subagent] 执行失败: {e}")
        return f"⚠️ 复杂任务执行失败，子Agent崩溃: {str(e)}"
