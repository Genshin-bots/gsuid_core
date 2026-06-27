"""Subagent 工具模块

提供创建子Agent的能力，允许AI搜索合适的System Prompt
并生成子Agent来完成特定任务，结果返回给主Agent。

## 三条委派路径

- ``create_subagent(task=...)``（无 agent_profile）：跑一个临时的通用
  Plan-and-Solve Agent，工具向量检索装配，**不挂任何 Kanban 树**。适合
  完全一次性、无产物、主人格自己直接对话回答用户的内部小步骤。
- ``create_subagent(task=..., agent_profile=...)``（默认 transient=False）：
  **自动转为创建一棵单子任务的 Kanban 叶子根树**——同步等待该子任务跑完，把
  代理返回值 + artifact 句柄拼成回执串返回给主人格。这条路径之所以走 Kanban：
    * 产物（PNG / 文件 / 报告）有 Kanban root_task_id 锚点，看板树视图能直接
      看到；点开任务即可在详情里看全部 artifact + workspace 文件。
    * 主人格事后用 `artifact_get_recent` 能自然找回该次执行的最近产物。
    * 与"主人格主动 register_kanban_task"路径共用同一套调度器 / 工作区 /
      产物登记机制，统一管理、避免双轨。
- ``create_subagent(task=..., agent_profile=..., transient=True)``：**绕过 Kanban**
  直接跑能力代理（含 profile 的工具集 + system_prompt），用于纯查询 / lookup 类
  任务——比如 "把 workspace 里的文件列出来"、"问 internal_reporter record 表当前
  状态" ——避免在看板上堆出无产物的"获取/查看/列出"任务卡。仅供 lookup 用，**任何
  生成文件 / 持久化状态的任务都必须保持 transient=False**。
"""

import asyncio
from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.rag.tools import search_tools
from gsuid_core.ai_core.session_registry import get_ai_session_registry
from gsuid_core.ai_core.configs.ai_config import ai_config

# 注意：create_agent 在 create_subagent() 内部懒加载导入，
# 避免 buildin_tools → subagent → gs_agent → persona → buildin_tools 的循环导入。

# 子Agent最大迭代次数上限，防止死循环
_SUBAGENT_MAX_ITERATIONS = 3

# 全局并发上限信号量：首个子Agent调用时按配置 subagent_max_concurrency 懒创建并缓存。
# 不在导入期读配置（此时配置可能未就绪）；改并发数需重启——给运行中的信号量改容量不安全。
_subagent_semaphore: Optional[asyncio.Semaphore] = None


def _get_subagent_semaphore() -> asyncio.Semaphore:
    global _subagent_semaphore
    if _subagent_semaphore is None:
        _subagent_semaphore = asyncio.Semaphore(ai_config.get_config("subagent_max_concurrency").data)
    return _subagent_semaphore


# create_subagent(agent_profile=...) 转 Kanban 路径同步等待的超时（秒）
# 主人格被这条工具阻塞，等代理跑完。超时后返回"任务仍在跑，到 webconsole 看进度"。
# 选 180 秒：覆盖 95% 的 code_agent / research_agent 单步任务；真要更长的任务
# 主人格应当主动用 register_kanban_task 显式建任务树，不走临时委派。
_KANBAN_INLINE_WAIT_TIMEOUT_SEC = 180.0
# 内部轮询的步长——任务完成会被 _run_one_task_node 写库，本侧轮询读出来即可
_KANBAN_INLINE_POLL_INTERVAL_SEC = 0.6


@ai_tools(category="common", capability_domain="长期任务编排")
async def create_subagent(
    ctx: RunContext[ToolContext],
    task: str,
    max_tokens: int = 35000,
    max_iterations: int = 15,  # 规划+执行通常需要较多轮次
    agent_profile: str = "",
    transient: bool = False,
) -> str:
    """
    处理复杂任务的终极工具。
    当用户的问题需要：多步拆解、深度调研、长时间执行、收集大量资料时调用此工具。
    它会自动创建一个具备“规划 -> 逐步执行 -> 校验 -> 总结”能力的自主Agent。

    Args:
        ctx: 工具执行上下文
        task: 需要完成的复杂任务描述。请把用户的原始意图清晰地转述在这里。
        agent_profile: 用自然语言描述需要哪类专职能力代理（"写代码""金融分析"
            "调研"）。指定后会派给对应的无人格能力代理执行；留空则用通用规划
            执行子Agent（保持原有泛化行为）。
        transient: **是否绕过 Kanban 直接跑临时任务**。默认 False——所有带
            `agent_profile` 的调用都会自动建一棵叶子根 Kanban 任务卡（产物可追溯、
            看板可见）。**只有当任务是纯粹的"读取 / 查询 / lookup"** —— 比如
            "把当前 workspace 里有哪些文件列出来"、"用 internal_reporter 把 record:
            stock:account 表读出来给我看一眼"、"问 research_agent 一句什么是 PB
            ratio" ——**才**传 True 跳过 Kanban，避免在看板上堆出一堆"获取/查看/列出"
            的无产物任务卡。**任何会生成文件 / 图片 / 报告 / 持久化状态变更的任务**
            都必须保持 transient=False（默认值）。

    **何时不要用 create_subagent**：
    - 任务需要 ≥ 2 步、跨能力代理接力、或周期触发 → 一律走 `register_kanban_task`。
    - 任务交付的产物要让主人事后追溯（"那张图呢""那个账户余额是多少"）→ 默认
      transient=False 会自动转 Kanban 叶子根，看板上有一张任务卡；不要传 True。
    - 简单的"问代理一个单点答案、不需要事后追溯" → 用 transient=True，跑完即丢。
    """
    # transient=True：直接走通用 Plan-and-Solve Agent，不创建 Kanban 任务卡。
    # 即使指定了 agent_profile 也不走 Kanban——这是用户显式宣告"只是 lookup，没产物
    # 需要追溯"。代理在 ad-hoc workspace 跑，结果文本直接返回主人格。
    if transient and agent_profile:
        return await _dispatch_transient_capability_agent(ctx, task, agent_profile)
    # agent_profile 非空 + transient=False（默认）→ 自动转为创建 Kanban 叶子根
    # 单任务并同步等待执行完成。
    # 实现见 _dispatch_via_kanban：所有"通过代理人格创建的、要产物追溯的任务"统一走 Kanban，
    # 产物会自动挂到该树的 root_task_id 下，看板树视图直接可见，主人格事后
    # 也能 artifact_get_recent 找回产物。
    if agent_profile:
        return await _dispatch_via_kanban(ctx, task, agent_profile)

    logger.info(f"🧠 [Subagent] 启动通用规划执行Agent，任务: {task[:50]}...")

    async with _get_subagent_semaphore():
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

        from gsuid_core.ai_core.gs_agent import create_agent

        task_hash = hashlib.md5(task.encode()).hexdigest()[:8]
        subagent_session_id = f"subagent_{task_hash}"
        agent = create_agent(
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            max_iterations=max_iterations,
            create_by="AutoPlanner",
            task_level="high",
            session_id=subagent_session_id,
            is_subagent=True,
        )

        # 将 SubAgent 注册到 AISessionRegistry，使其在运行期间可被内存查找
        _session_registry = get_ai_session_registry()
        _session_registry.set_ai_session(subagent_session_id, agent)

        # 建立主 Agent ↔ SubAgent 的关联（双向）。
        # _session_logger / _file_path 都是已声明字段（见 gs_agent.GsCoreAIAgent.__init__
        # 与 session_logger.AISessionLogger.__init__），直接访问 + None 判断即可，
        # 无需 getattr 兜底（LLM.md §1.4）。
        try:
            parent_session_id = ctx.deps.ev.session_id if ctx.deps.ev else None
            if parent_session_id:
                parent_session = _session_registry.get_ai_session(parent_session_id)
                if parent_session is not None:
                    parent_logger = parent_session._session_logger
                    sub_logger = agent._session_logger
                    if parent_logger is not None and sub_logger is not None:
                        sub_log_file = str(sub_logger._file_path)
                        # 主 Agent 记录关联的 SubAgent（含日志文件路径）
                        parent_logger.link_agent(
                            agent_session_id=subagent_session_id,
                            agent_session_uuid=sub_logger.session_uuid,
                            agent_type="sub_agent",
                            persona_name=agent.persona_name,
                            create_by=agent.create_by,
                            log_file=sub_log_file,
                        )
                        parent_log_file = str(parent_logger._file_path)
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
            # SubAgent 执行完毕（无论成功或异常），确保日志落盘并从 AISessionRegistry 移除。
            # agent._session_logger 是 GsCoreAIAgent 已声明字段（可能为 None），直接访问。
            # remove_ai_session 内部是幂等的（不存在返回 False，不抛异常），无需 try/except。
            if agent._session_logger is not None:
                agent._session_logger.close()
            _session_registry.remove_ai_session(subagent_session_id)


async def _dispatch_transient_capability_agent(
    ctx: RunContext[ToolContext],
    task: str,
    agent_profile: str,
) -> str:
    """transient=True 路径：直接跑能力代理（含 agent_profile 的工具集 + 系统提示词），
    走 ad-hoc workspace、**不创建** Kanban 任务卡。

    适用于纯查询 / lookup 任务——主人格不需要事后追溯产物（"这次只是问一下"），
    或框架阶段性需要让代理人格做轻量内部调度（如让 internal_reporter 临时读一下
    某个 record_* 集合的状态摘要）。

    与 `_dispatch_via_kanban` 的关键差别：
    - 不调 `kanban.create_kanban_tree` → 看板上看不到这条调度；
    - workspace 是 ad-hoc（`adhoc_<sess>/adhoc_<profile>_<ts>_<rand>/workspace/`）；
    - 产物 artifact 仍登记到数据库（root_task_id 是 ad-hoc 字符串前缀），但
      `artifact_get_recent` 不会从主人当前活跃根任务里拉到它们；
    - 同步阻塞主人格直到代理跑完（或抛错）；
    - 调用响应给主人格的文本里**显式**标注"transient 模式 / 看板无对应卡片"，
      避免主人格之后再去看板找。

    transient 模式严禁用于"会生成持久化产物"的任务——这是工具 docstring 已声明的红线；
    主人格 prompts 也会重申。
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无法获取会话信息，create_subagent(transient=True) 派发失败。"

    from gsuid_core.ai_core.capability_agents.runner import (
        CAPABILITY_AGENT_ERROR_PREFIX,
        run_capability_agent,
    )
    from gsuid_core.ai_core.capability_agents.registry import (
        get_profile,
        resolve_profile,
    )

    pid = resolve_profile(agent_profile)
    profile = get_profile(pid)
    if profile is None:
        return f"⚠️ 能力代理画像不存在: {agent_profile}（解析为 {pid}）"

    logger.info(f"🧠 [Subagent] transient 模式直跑 profile={pid} task={task[:60]!r}")
    try:
        # runner._ensure_adhoc_workspace contextmanager 会在无 plan_ctx 时建临时
        # ad-hoc workspace；这里直接调 run_capability_agent，让 runner 自己处理。
        raw_result = await run_capability_agent(
            profile_id=pid,
            task=task,
            ev=ev,
            bot=ctx.deps.bot,
            session_id_suffix=f"transient_{pid}",
        )
    except Exception as e:
        logger.exception(f"🧠 [Subagent] transient 代理执行异常: {e}")
        return f"⚠️ {pid} 临时代理执行失败: {type(e).__name__}: {e}"

    prefix_note = (
        f"【{pid} 临时代理已完成 / transient 模式】"
        "（**未在看板创建任务卡**——本次为 lookup 模式，产物不挂 Kanban、事后"
        "无法 artifact_get_recent 追溯。若需要可追溯的产物，请改用 transient=False。）"
    )
    if (raw_result or "").startswith(CAPABILITY_AGENT_ERROR_PREFIX):
        return f"{prefix_note}\n\n{raw_result}"
    return f"{prefix_note}\n\n{raw_result}"


async def _dispatch_via_kanban(
    ctx: RunContext[ToolContext],
    task: str,
    agent_profile: str,
) -> str:
    """把 create_subagent(agent_profile=...) 转为创建 Kanban **单任务**（叶子根）
    并同步等待执行完成。

    每条主人格通过画像派出的任务都走这条路：
    1. ``kanban.create_kanban_tree(root_agent_profile=pid)`` 建一棵**只有根任务**
       的叶子树——根任务自身带 ``agent_profile``，被调度器当作单一可执行节点直接
       派出。**不再**创建冗余的"根 + 1 子任务"双节点结构；
    2. ``kick_root`` 立刻派活；
    3. 轮询数据库等根任务进终态（completed / failed / waiting_approval 等）；
    4. 抓根任务最新产出 artifact 句柄 + relay 文本，拼成回执给主人格。

    超时（``_KANBAN_INLINE_WAIT_TIMEOUT_SEC``）后**不强制中止**——任务会继续在
    Kanban 调度器里跑，主人格收到提示"任务仍在跑，到 webconsole 看进度"，并被告知
    该 Kanban 任务 id 以便后续 `artifact_get_recent` 追问。
    """
    ev = ctx.deps.ev
    if ev is None:
        return "⚠️ 无法获取会话信息，create_subagent 派发失败。"

    from gsuid_core.ai_core.capability_agents.registry import (
        get_profile,
        resolve_profile,
    )

    pid = resolve_profile(agent_profile)
    profile = get_profile(pid)
    if profile is None:
        return f"⚠️ 能力代理画像不存在: {agent_profile}（解析为 {pid}）"

    # 拼一个简短的根目标——用任务原文前 96 字，足够 evaluator / 看板辨识
    root_goal = task[:96].replace("\n", " ").strip() or f"{profile.display_name} 临时任务"

    persona_name: Optional[str] = None
    try:
        from gsuid_core.ai_core.persona import persona_config_manager

        persona_name = persona_config_manager.get_persona_for_session(ev.session_id)
    except ImportError:
        pass

    from gsuid_core.ai_core.planning import kanban
    from gsuid_core.ai_core.memory.scope import ScopeType, make_scope_key
    from gsuid_core.ai_core.planning.models import AIAgentTask, AIAgentArtifact
    from gsuid_core.ai_core.planning.kanban_executor import kick_root

    scope_key = make_scope_key(
        ScopeType.GROUP if ev.group_id else ScopeType.USER_GLOBAL,
        str(ev.group_id or ev.user_id),
    )

    # 叶子根模式：根任务自身就是执行节点，不挂子任务
    root, _children = await kanban.create_kanban_tree(
        goal=task,  # 根任务 goal 直接用任务原文（叶子根没子任务，goal 是执行体的任务描述）
        owner_user_id=str(ev.user_id),
        scope_key=scope_key,
        bot_id=ev.bot_id,
        persona_name=persona_name,
        bot_self_id=ev.bot_self_id or "",
        group_id=ev.group_id,
        user_type=ev.user_type or "direct",
        WS_BOT_ID=ev.WS_BOT_ID,
        session_id=ev.session_id,
        # 透传派活人的权限等级——否则 Kanban 执行体重建 Event 后退回默认 6，
        # 主人（pm=0）派出的 plugin_dev 代理会被自家 check_pm 工具全部拒绝。
        user_pm=ev.user_pm,
        broadcast_targets=[],
        display_name=root_goal[:64],
        subtasks=[],
        root_agent_profile=pid,
    )

    logger.info(
        f"🧠 [Subagent] 转 Kanban 叶子根：root#{root.ordinal} id={root.id[:6]} profile={pid} task={task[:60]!r}"
    )
    asyncio.create_task(kick_root(root.id))

    # 同步等待根任务进终态（轮询）
    waited = 0.0
    final: Optional[AIAgentTask] = None
    while waited < _KANBAN_INLINE_WAIT_TIMEOUT_SEC:
        await asyncio.sleep(_KANBAN_INLINE_POLL_INTERVAL_SEC)
        waited += _KANBAN_INLINE_POLL_INTERVAL_SEC
        fresh = await AIAgentTask.get_by_id(root.id)
        if fresh is None:
            return f"⚠️ Kanban 任务记录消失（task_id={root.id[:8]}）；可能被并发删除，请到 webconsole 看任务列表。"
        if fresh.status in ("completed", "failed", "cancelled", "waiting_approval"):
            final = fresh
            break

    if final is None:
        return (
            f"⏳ 任务仍在执行中（已等待 {int(waited)}s 超时）。\n"
            f"Kanban 任务: 任务#{root.ordinal}｜{root.display_name}\n"
            f"任务 id（前 8 位）: {root.id[:8]}\n"
            "可到 webconsole 看板查看实时进度；事后追问产物用 "
            "`artifact_get_recent` 即可（已绑定本任务树）。"
        )

    # 抓 artifact（最新一份用作产物展示）
    arts = await AIAgentArtifact.list_for_task(final.id)
    art_lines = []
    primary_handle = ""
    for a in arts[:5]:
        binary_tag = ""
        if a.payload_path and (a.mime or "").startswith("image/"):
            binary_tag = "（真实图片，可 send_message_by_ai(image_id=) 直发）"
        elif a.payload_path:
            binary_tag = "（落盘文件）"
        art_lines.append(f"  - {a.id} | {a.mime or 'text/plain'} | {a.summary[:80]}{binary_tag}")
        if not primary_handle and a.payload_path and (a.mime or "").startswith("image/"):
            primary_handle = a.id
    if not primary_handle and arts:
        primary_handle = arts[0].id

    status_label = {
        "completed": "✅ 已完成",
        "failed": "❌ 失败",
        "cancelled": "🚫 已取消",
        "waiting_approval": "⏸️ 等待审批",
    }.get(final.status, final.status)

    parts = [
        f"【{pid} 代理完成 - Kanban 任务#{root.ordinal}】 {status_label}",
        f"任务: {root.display_name}",
    ]
    if final.failure_reason:
        parts.append(f"失败原因: {final.failure_reason[:300]}")
    if art_lines:
        parts.append("产物 artifact:")
        parts.extend(art_lines)
        if primary_handle:
            parts.append(
                f"💡 主要产物句柄: `{primary_handle}`"
                "（要发图直接 send_message_by_ai(image_id=该句柄)；"
                "kanban_executor 已经用人格口吻转译播报过一次，主人通常已收到）"
            )
    else:
        parts.append("（本任务无显式 artifact 登记）")

    # 文本结论：从 inline 文本 artifact 里抓一段给主人格做"事后追问"参考。
    # 真实图片 / 落盘文件已经被 _persona_relay 自动转译播报过，主人通常已收到。
    text_excerpt = ""
    for a in arts:
        if a.payload_inline:
            text_excerpt = a.payload_inline[:1200]
            break
    if text_excerpt:
        parts.append(f"\n代理文本结论摘要:\n{text_excerpt}")
    return "\n".join(parts)
