"""Subagent 工具模块

提供创建子Agent的能力，允许AI搜索合适的System Prompt
并生成子Agent来完成特定任务，结果返回给主Agent。
"""

import asyncio

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.history import get_history_manager
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.rag.tools import search_tools

# 子Agent最大迭代次数上限，防止死循环
_SUBAGENT_MAX_ITERATIONS = 3

# 全局限制：同时最多 3 个 Subagent 并发运行
_subagent_semaphore = asyncio.Semaphore(3)


@ai_tools(category="self")
async def create_subagent(
    ctx: RunContext[ToolContext],
    task: str,
    max_tokens: int = 35000,
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

    async with _subagent_semaphore:
        # 搜索工具
        tools = await search_tools(
            query=task,
            limit=8,
            non_category="self",
        )
        # 子Agent不能再创建子Agent，防止递归爆炸
        tools = [t for t in tools if t.name != "create_subagent"]
        logger.debug(f"🧠 [Subagent] 工具列表: {[tool.name for tool in tools]}")

        # ✨ 内置一个 Plan-and-Solve System Prompt
        system_prompt = """
        你是一个极其聪明且自主的"规划与执行专家（Plan-and-Solve Agent）"。
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
            如果没有合适的工具则调用`list_skills`搜索可用技能,
            如果skill列表也没有合适的工具**才考虑**调用web_search工具去实际搜索。
        【注意】：技能 (Skills):
            - 当你想要使用`run_skill_script`工具调用技能之前, 你**必须**确保没有其他工具（Tools）可用
            - 当你想要使用`run_skill_script`工具调用技能之前, 你**必须**先调用`list_skills`获取当前可用技能
            - 如果`list_skills`返回空值, 则禁止调用`run_skill_script`
            - 在调用技能前优先检索其他可用工具（Tool）和知识库, 技能列表并非你的全部工具！
        """

        import hashlib

        task_hash = hashlib.md5(task.encode()).hexdigest()[:8]
        subagent_session_id = f"subagent_{task_hash}"
        from gsuid_core.ai_core.gs_agent import create_agent  # 延迟导入避免循环导入

        agent = create_agent(
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            max_iterations=max_iterations,
            create_by="AutoPlanner",
            task_level="high",
            session_id=subagent_session_id,
            is_subagent=True,
        )

        # 将 SubAgent 注册到 HistoryManager，使其在运行期间可被内存查找
        _history_manager = get_history_manager()
        _history_manager.set_ai_session(subagent_session_id, agent)

        # 建立主 Agent ↔ SubAgent 的关联（双向）
        try:
            parent_session_id = ctx.deps.ev.session_id if ctx.deps.ev else None
            if parent_session_id:
                parent_session = _history_manager.get_ai_session(parent_session_id)
                if parent_session is not None:
                    parent_logger = getattr(parent_session, "_session_logger", None)
                    sub_logger = getattr(agent, "_session_logger", None)
                    if parent_logger is not None and sub_logger is not None:
                        sub_log_file = str(getattr(sub_logger, "_file_path", ""))
                        # 主 Agent 记录关联的 SubAgent（含日志文件路径）
                        parent_logger.link_agent(
                            agent_session_id=subagent_session_id,
                            agent_session_uuid=sub_logger.session_uuid,
                            agent_type="sub_agent",
                            persona_name=agent.persona_name,
                            create_by=agent.create_by,
                            log_file=sub_log_file,
                        )
                        parent_log_file = str(getattr(parent_logger, "_file_path", ""))
                        # SubAgent 记录关联的父 Agent（预留 parent_agent 类型）
                        sub_logger.link_agent(
                            agent_session_id=parent_session_id,
                            agent_session_uuid=parent_logger.session_uuid,
                            agent_type="parent_agent",
                            persona_name=parent_session.persona_name,
                            create_by=parent_session.create_by,
                            log_file=parent_log_file,
                        )
                        logger.info(
                            f"🧠 [Subagent] 建立 Agent 关联: {parent_session_id}({parent_logger.session_uuid}) "
                            f"-> {subagent_session_id}({sub_logger.session_uuid})"
                        )
        except Exception as link_err:
            logger.warning(f"🧠 [Subagent] 建立 Agent 关联失败（非致命）: {link_err}")

        try:
            # 直接把任务扔给它，它会被 system_prompt 逼着去先列 TODO list
            result = await agent.run(
                user_message=f"【当前任务】\n{task}\n\n请立即开始你的规划与执行！",
                bot=ctx.deps.bot,
                ev=ctx.deps.ev,
                tools=tools,
                return_mode="return",  # 结果返回给主Agent，由主Agent决定何时发送给用户
            )

            return f"【子Agent规划并执行完毕，交付结果如下】\n\n{result}"

        except Exception as e:
            logger.error(f"❌[Subagent] 执行失败: {e}")
            return f"⚠️ 复杂任务执行失败，子Agent崩溃: {str(e)}"
        finally:
            # SubAgent 执行完毕（无论成功或异常），确保日志落盘并从 HistoryManager 移除
            if hasattr(agent, "_session_logger") and agent._session_logger is not None:
                agent._session_logger.close()
            try:
                _history_manager.remove_ai_session(subagent_session_id)
            except Exception:
                pass
