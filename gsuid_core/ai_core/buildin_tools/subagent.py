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

import re
import asyncio
from typing import Optional

from pydantic_ai import RunContext

from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.rag.tools import search_tools
from gsuid_core.ai_core.session_registry import get_ai_session_registry
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.control.delegation import await_delegation, delegation_handle

# 注意：create_agent 在 create_subagent() 内部懒加载导入
# 避免 buildin_tools → subagent → gs_agent → persona → buildin_tools 的循环导入。

# 子Agent最大迭代次数上限，防止死循环
_SUBAGENT_MAX_ITERATIONS = 3

# 能力代理返回「只有过程句、无事实包」时再催一次交付（避免无限递归）
_INCOMPLETE_DELIVERY_MARKERS = (
    "停止重复",
    "下面再",
    "再做几次",
    "然后再",
    "然后渲染",
    "先补充",
    "先检索",
    "我去翻",
    "正在搜索",
    "继续搜索",
    "稍后",
    "接下来会",
    "马上整理",
)
_TRANSIENT_PREFIX_RE = re.compile(
    r"^【[^】]*临时代理已完成[^】]*】[^\n]*\n*",
    re.MULTILINE,
)


def _strip_transient_wrapper(text: str) -> str:
    """去掉 create_subagent 返回前缀，便于判空。"""
    s = (text or "").strip()
    if not s:
        return ""
    return _TRANSIENT_PREFIX_RE.sub("", s).strip()


# 已登记产物句柄：有 res_ 即视为可消费交付（勿被 OOC 误杀后再判 incomplete）
_RES_HANDLE_RE = re.compile(r"\bres_[0-9a-fA-F]{6,}\b")
_ARTIFACT_REGISTERED_RE = re.compile(
    r"(已登记\s*artifact|artifact[_\s-]?put|事实包已登记|登记为\s*\*?`?res_)",
    re.IGNORECASE,
)


def looks_like_incomplete_subagent_delivery(text: str) -> bool:
    """能力代理是否只回了过程句 / 空壳，没有可消费的事实包。

    形状判据：过短且无结构，或命中过程口癖且无表格/列表/JSON/多段落。
    有 res_ 句柄或 artifact 登记声明 → 一律视为完整（深度调研常把正文放 artifact）。
    """
    body = _strip_transient_wrapper(text)
    if not body:
        return True
    # 错误前缀：已是失败语义，主路径另处理，不视为「可再催」的空过程句
    if body.startswith("⚠️") or "执行失败" in body[:40]:
        return False
    # 成功交付硬信号：句柄 / 登记声明（优先于过程口癖）
    if _RES_HANDLE_RE.search(body) or _ARTIFACT_REGISTERED_RE.search(body):
        return False
    has_structure = (
        "|" in body
        or "```" in body
        or body.lstrip().startswith("{")
        or body.lstrip().startswith("[")
        or body.count("\n") >= 5
        or len(re.findall(r"(?m)^\s*[-*•]|\d+[\.、]\s+\S", body)) >= 3
    )
    if has_structure and len(body) >= 120:
        return False
    if any(m in body for m in _INCOMPLETE_DELIVERY_MARKERS) and not has_structure:
        return True
    # 无结构且过短：几乎一定是过程句
    if not has_structure and len(body) < 160:
        return True
    return False


def _delivery_followup_task(original_task: str) -> str:
    """催收事实包：要求基于已检索结果立即交付，禁止过程句与 render。"""
    ot = (original_task or "").strip()
    if len(ot) > 1200:
        ot = ot[:1200] + "…"
    return (
        "【交付催收·硬门】上一轮你未交付可消费的事实包（仅过程句或空输出）。\n"
        f"原任务：\n{ot}\n\n"
        "要求：基于你**已经检索到的信息**（不要再空转同一工具），**立即**输出完整 "
        "Markdown 或 JSON 事实包：\n"
        "① 条目列表（日期、事件、关键数字、为何重要、来源 URL、**数据时点**）\n"
        "② 依据（工具/字段/URL）\n"
        "③ 可选：主线摘要与风险提示\n"
        "缺来源或时点须补查或标「信息可能过时/时点未知」；"
        "禁止只说「下面再搜 / 停止重复 / 然后渲染」；"
        "禁止 render_*（出图由主人格再委派 render_agent）；"
        "长文可用 artifact_put。若确实零数据，写「无检索结果：原因=…」。"
    )


def _main_persona_receipt_hint(*, image_likely: bool = False) -> str:
    """回执里给主人格的固定口吻（不诱导自渲）。"""
    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        RENDER_DONE_RECEIPT_MARK,
    )

    if image_likely:
        return (
            f"【工具通道】{RENDER_DONE_RECEIPT_MARK}；"
            "【聊天通道】发图后至多一句角色口吻；禁止念工具名/句柄/节点名/流程；"
            "禁止把代理全文当群聊台词；主语永远是你自己。"
        )
    return (
        "【工具通道】长结构化结果再 "
        'create_subagent(agent_profile="render_agent", task=事实包或句柄) 出图；'
        "禁止自写 HTML / 直调 render_*；出图委派**不要**再对用户说话。"
        "【聊天通道】委派长任务前须已说一句「得等一会儿」；"
        "子任务在途除等待句外 <SILENCE>；未发图勿说「图好了」；"
        "禁止把代理全文当台词；禁止对用户提节点名/句柄/「让某某去画」。"
    )


_DATEISH_RE = re.compile(
    r"(20\d{2}[-/.年]\d{1,2}([-/.月]\d{1,2})?|\d{1,2}\s*月|Q[1-4]|时点|截至|as of|fetched)",
    re.I,
)
_URLISH_RE = re.compile(r"https?://|www\.|来源|依据|工具", re.I)


def _factpack_freshness_note(body: str) -> str:
    """轻量启发式：疑缺来源/时点时附在回执（不注入主 system）。"""
    text = (body or "").strip()
    if len(text) < 80:
        return ""
    if _URLISH_RE.search(text) and _DATEISH_RE.search(text):
        return ""
    missing: list[str] = []
    if not _URLISH_RE.search(text):
        missing.append("来源")
    if not _DATEISH_RE.search(text):
        missing.append("时点")
    if not missing:
        return ""
    return (
        f"\n⚠ 事实包疑缺{'/'.join(missing)}：出图前可要求 research 补查，"
        "或在 render_agent 的 task 里标明「信息可能过时」。"
    )


# 全局并发上限信号量：首个子Agent调用时按配置 subagent_max_concurrency 懒创建并缓存。
# 不在导入期读配置（此时配置可能未就绪）；改并发数需重启——给运行中的信号量改容量不安全。
_subagent_semaphore: Optional[asyncio.Semaphore] = None


def _get_subagent_semaphore() -> asyncio.Semaphore:
    global _subagent_semaphore
    if _subagent_semaphore is None:
        _subagent_semaphore = asyncio.Semaphore(ai_config.get_config("subagent_max_concurrency").data)
    return _subagent_semaphore


# create_subagent(agent_profile=...) 转 Kanban：短等快速完成，否则 deferred 回灌。
# 短等上限须低于会话 _run_lock 排队 STALE，避免长任务占锁导致群聊应答率塌陷。
_KANBAN_INLINE_WAIT_TIMEOUT_SEC = 5.0
# 轮询间隔已收敛到 control.delegation.await_delegation；此处保留常量仅为兼容引用
_KANBAN_INLINE_POLL_INTERVAL_SEC = 0.5

# 纯 lookup 默认同步 ad-hoc（transient），不建看板卡。
# 外部检索默认 Kanban：超时可回灌，取消不会把事实包扔掉。
_TRANSIENT_DEFAULT_PROFILES = frozenset(
    {
        "internal_reporter",
        "memory_curator",
        "scheduler_assistant",
    }
)


@ai_tools(category="common", capability_domain="长期任务编排", timeout=500.0)
async def create_subagent(
    ctx: RunContext[ToolContext],
    task: str,
    max_tokens: int = 35000,
    max_iterations: int = 15,  # 规划+执行通常需要较多轮次
    agent_profile: str = "",
    transient: bool = False,
) -> str:
    """
    委派专职能力代理（或通用 Plan-and-Solve 子 Agent）执行多步任务。

    ## 路由（agent_profile 填 node_id，禁止自造名）
    - ``research_agent``：外部检索 / 综合分析 → **只交事实包**（来源+时点）
    - ``render_agent``：把**已有**事实包渲成美观信息图（多项数据出图**必走**；主人格禁自渲）
    - ``code_agent``：写代码 / PIL·脚本真文件产物（不是 HTML 信息卡）
    - ``internal_reporter`` / ``memory_curator`` / ``scheduler_assistant`` / …
      见本轮 system 能力清单

    ## task 写作
    - 检索综合：目标 + 范围；交付须含条目/数字/**来源**/**时点**。
    - 出图：粘贴完整事实包（或 res_ 句柄）+ 可选版式偏好；写明**禁止再检索**。
    - 禁止把「漂亮出图」派给 research；禁止主人格自己写 HTML 调 render_*。
    - 委派前后默认不对用户说话；短应走正文或 `<SILENCE>`，不要用本工具报过程。

    Args:
        ctx: 工具执行上下文
        task: 任务全文（事实包请直接写进 task，勿只写「帮我出图」）。
        agent_profile: 能力代理 node_id 或可 resolve 的自然语言；空=通用规划子 Agent。
        transient: True 仅纯 lookup；出图/落盘/改状态必须 False（默认）。

    **何时不要用 create_subagent**：
    - ≥2 能力接力或周期任务 → ``register_kanban_task``。
    - 要事后追溯产物 → 默认 transient=False。
    """
    # 子代理墙钟不计入主人格 soft budget（research 常 >45s，否则触发禁工具→无法 render）
    from gsuid_core.ai_core.wall_clock import pause_wall_clock

    async with pause_wall_clock():
        raw = await _create_subagent_impl(
            ctx,
            task=task,
            max_tokens=max_tokens,
            max_iterations=max_iterations,
            agent_profile=agent_profile,
            transient=transient,
        )
        head = (task or "").strip().split("\n", 1)[0][:80]
        if ctx.deps is not None:
            from gsuid_core.ai_core.outbound import write_decision_memo, remember_outbound_topic

            remember_outbound_topic(ctx.deps.extra, head)
            ev = ctx.deps.ev
            await write_decision_memo(
                bot_self_id=str(ev.bot_self_id) if ev is not None and ev.bot_self_id else "",
                text=f"委派 {head[:40]}",
                ref=f"decision:sub:{head[:40]}"[:160],
                owner_user_id=str(ev.user_id) if ev is not None and ev.user_id else "",
            )
        body = f"（委派原问：{head}）\n{raw}" if head else raw
        return await _maybe_fold_subagent_receipt(ctx, body)


async def _maybe_fold_subagent_receipt(ctx: RunContext[ToolContext], text: str) -> str:
    """回执 >1500 字落 FileOS 折句柄卡，堵整包回灌。"""
    if len(text) <= 1500:
        return text
    from gsuid_core.ai_core.planning.tool_output_helper import persist_and_fold_tool_return

    ev = ctx.deps.ev if ctx.deps is not None else None
    session_id = ""
    if ctx.deps is not None and ctx.deps.parent_session_id:
        session_id = ctx.deps.parent_session_id
    card = await persist_and_fold_tool_return(
        "create_subagent",
        text,
        ev,
        session_id,
        is_group=bool(ev and ev.group_id),
    )
    return card if card else text[:1500] + "\n…[过长已截断，详见句柄]"


async def summarize_long_input(text: str, *, max_tokens: int = 18000) -> str:
    """内核长度防护：无工具上下文的摘要委派（不是模型可调的 ``create_subagent``）。"""
    from gsuid_core.ai_core.wall_clock import pause_wall_clock

    async with pause_wall_clock():
        return await _create_subagent_impl(
            None,
            task=f"请总结以下用户输入，保留关键信息：\n\n{text}",
            max_tokens=max_tokens,
            max_iterations=15,
            agent_profile="",
            transient=True,
        )


async def _create_subagent_impl(
    ctx: RunContext[ToolContext] | None,
    *,
    task: str,
    max_tokens: int,
    max_iterations: int,
    agent_profile: str,
    transient: bool,
) -> str:
    """create_subagent 实现体（已在 pause_wall_clock 内）。``ctx is None`` 仅内核摘要用。"""
    # 无 ctx 的内核摘要不走能力路由，避免摘要任务被误派到专域节点。
    if ctx is not None and agent_profile:
        from gsuid_core.ai_core.agent_node import resolve_node

        pid = resolve_node(agent_profile) or agent_profile.strip()
        use_transient = transient or pid in _TRANSIENT_DEFAULT_PROFILES
        if use_transient:
            return await _dispatch_transient_capability_agent(ctx, task, agent_profile)
        return await _dispatch_via_kanban(ctx, task, agent_profile)

    if ctx is not None and not agent_profile:
        from gsuid_core.ai_core.agent_node import get_node, match_capability_node

        auto_pid = match_capability_node(task)
        if auto_pid and get_node(auto_pid) is not None:
            logger.info(
                i18n_t("log.ai.subagent_convert_kanban_leaf", p0=0, p1=auto_pid[:6], pid=auto_pid, p2=repr(task[:60]))
            )
            use_transient = transient or auto_pid in _TRANSIENT_DEFAULT_PROFILES
            if use_transient:
                return await _dispatch_transient_capability_agent(ctx, task, auto_pid)
            return await _dispatch_via_kanban(ctx, task, auto_pid)

    logger.info(i18n_t("log.ai.subagent_general_planning_executor_start", p0=task[:50]))

    async with _get_subagent_semaphore():
        # 搜索工具
        tools = await search_tools(
            query=task,
            limit=8,
            non_category="self",
        )
        # 子Agent不能再创建子Agent，防止递归爆炸
        tools = [t for t in tools if t.name != "create_subagent"]
        logger.debug(i18n_t("log.ai.subagent_tool_list", p0=[tool.name for tool in tools]))

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
        try:
            parent_session_id = ctx.deps.ev.session_id if ctx is not None and ctx.deps.ev else None
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
                            i18n_t(
                                "log.ai.subagent_establishing_agent_link",
                                parent_session_id=parent_session_id,
                                p0=parent_logger.session_uuid,
                                subagent_session_id=subagent_session_id,
                                p1=sub_logger.session_uuid,
                            )
                        )
        except Exception as link_err:
            logger.warning(i18n_t("log.ai.subagent_establish_agent_link_fail", link_err=link_err))

        try:
            # 直接把任务扔给它，它会被 system_prompt 逼着去先列 TODO list
            result = await agent.run(
                user_message=f"【当前任务】\n{task}\n\n请立即开始你的规划与执行！",
                bot=ctx.deps.bot if ctx is not None else None,
                ev=ctx.deps.ev if ctx is not None else None,
                tools=tools,
                return_mode="return",  # 结果返回给主Agent，由主Agent决定何时发送给用户
            )

            return f"【子Agent交付完毕】{_main_persona_receipt_hint()}\n\n{result}"

        except Exception as e:
            logger.error(i18n_t("log.ai.subagent_fail_execution_failed", e=e))
            return f"⚠️ 复杂任务执行失败，子Agent崩溃: {str(e)}"
        finally:
            # SubAgent 执行完毕（无论成功或异常），确保日志落盘并从 AISessionRegistry 移除。
            # agent._session_logger 是 GsCoreAIAgent 已声明字段（可能为 None），直接访问。
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

    from gsuid_core.ai_core.agent_node import get_node, resolve_node
    from gsuid_core.ai_core.capability_agents.runner import (
        CAPABILITY_AGENT_ERROR_PREFIX,
        run_capability_agent,
    )

    pid = resolve_node(agent_profile)
    profile = get_node(pid)
    if profile is None:
        from gsuid_core.ai_core.agent_node import list_nodes

        avail = ", ".join(
            f"{n.node_id}({n.display_name})"
            for n in list_nodes()
            if n.source != "persona" and n.node_id != "capability_evaluator"
        )
        return (
            f"⚠️ 能力代理节点不存在: {agent_profile}（解析为 {pid or '空'}）。"
            f"请改用下列 node_id 之一：{avail or '（当前无已注册能力代理）'}"
        )

    logger.info(i18n_t("log.ai.subagent_transient_mode_direct", pid=pid, p0=repr(task[:60])))
    try:
        # runner._ensure_adhoc_workspace contextmanager 会在无 plan_ctx 时建临时 ad-hoc workspace；
        # 这里直接调 run_capability_agent，让 runner 自己处理。
        raw_result = await run_capability_agent(
            profile_id=pid,
            task=task,
            ev=ev,
            bot=ctx.deps.bot,
            session_id_suffix=f"transient_{pid}",
        )
    except Exception as e:
        logger.exception(i18n_t("log.ai.subagent_transient_agent_fail", e=e))
        return f"⚠️ {pid} 临时代理执行失败: {type(e).__name__}: {e}"

    # 空/过程句：再与 subagent 对话一次，要求交出事实包（仅 1 次）
    if looks_like_incomplete_subagent_delivery(raw_result or ""):
        first_preview = repr((raw_result or "")[:80])
        logger.warning(
            i18n_t(
                "log.ai.create_subagent_incomplete_delivery",
                pid=pid,
                preview=first_preview,
            )
        )
        first_raw = raw_result
        try:
            raw_result = await run_capability_agent(
                profile_id=pid,
                task=_delivery_followup_task(task),
                ev=ev,
                bot=ctx.deps.bot,
                session_id_suffix=f"transient_{pid}_retry",
            )
        except Exception as e:
            logger.exception(i18n_t("log.ai.create_subagent_delivery_requery_fail", e=e))
            raw_result = first_raw
        if looks_like_incomplete_subagent_delivery(raw_result or ""):
            logger.warning(i18n_t("log.ai.create_subagent_still_incomplete", pid=pid))

    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        receipt_image_likely,
    )

    image_likely = receipt_image_likely(pid=pid, has_image_art=False)
    prefix_note = (
        f"【{pid} 临时代理已完成 / transient 模式】"
        "（**未在看板创建任务卡**——lookup 模式。）"
        f"{_main_persona_receipt_hint(image_likely=image_likely)}"
    )
    if (raw_result or "").startswith(CAPABILITY_AGENT_ERROR_PREFIX):
        return f"{prefix_note}\n\n{raw_result}"
    if looks_like_incomplete_subagent_delivery(raw_result or ""):
        return (
            f"{prefix_note}\n\n"
            f"⚠️ 子代理未交付可用事实包（过程句/空输出）。"
            f"请主人格改用 web_search_tool 自行补查，或再次 create_subagent 并收紧 task。"
            f"\n\n【子代理原文】\n{(raw_result or '').strip() or '（空）'}"
        )
    note = _factpack_freshness_note(raw_result or "") if pid == "research_agent" else ""
    return f"{prefix_note}\n\n{raw_result}{note}"


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

    from gsuid_core.ai_core.agent_node import get_node, resolve_node

    pid = resolve_node(agent_profile)
    profile = get_node(pid)
    if profile is None:
        from gsuid_core.ai_core.agent_node import list_nodes

        avail = ", ".join(
            f"{n.node_id}({n.display_name})"
            for n in list_nodes()
            if n.source != "persona" and n.node_id != "capability_evaluator"
        )
        return (
            f"⚠️ 能力代理节点不存在: {agent_profile}（解析为 {pid or '空'}）。"
            f"请改用下列 node_id 之一：{avail or '（当前无已注册能力代理）'}"
        )

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
    from gsuid_core.ai_core.planning.kanban_executor import (
        kick_root,
        mark_deferred_main_delivery,
        mark_interactive_relay_root,
        try_claim_deferred_for_inline_return,
    )

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
        # 透传派活人的权限等级——否则 Kanban 执行体重建 Event 后退回默认 6
        # 主人（pm=0）派出的 plugin_dev 代理会被自家 check_pm 工具全部拒绝。
        user_pm=ev.user_pm,
        broadcast_targets=[],
        display_name=root_goal[:64],
        subtasks=[],
        root_agent_profile=pid,
    )

    # 任务正文里的 res_ 句柄 → input_artifact_ids（调研→渲染跨叶子树交接）
    from gsuid_core.ai_core.planning.kanban_tools import extract_res_ids

    handoff_ids = extract_res_ids(task)
    if handoff_ids:
        await AIAgentTask.update_data_by_data(
            select_data={"id": root.id},
            update_data={"input_artifact_ids": handoff_ids},
        )
        root.input_artifact_ids = handoff_ids

    logger.info(
        i18n_t(
            "log.ai.subagent_convert_kanban_leaf",
            p0=root.ordinal,
            p1=root.id[:6],
            pid=pid,
            p2=repr(task[:60]),
        )
    )
    # 登记为"主人格转述"：交互式派发下，执行体（kanban_executor）**不自动推群**，
    mark_interactive_relay_root(root.id)
    asyncio.create_task(kick_root(root.id))

    # 同步等待根任务进终态：与 check_delegation 共用 await_delegation，
    # 「内联等 5s」因此只是同一入口的默认参数，不再是独立轮询路径。
    waited = _KANBAN_INLINE_WAIT_TIMEOUT_SEC
    deleg = await await_delegation(delegation_handle(root.id), wait_sec=_KANBAN_INLINE_WAIT_TIMEOUT_SEC)
    if deleg is None:
        return f"⚠️ Kanban 任务记录消失（task_id={root.id}）；可能被并发删除，请到 webconsole 看任务列表。"
    final: Optional[AIAgentTask] = await AIAgentTask.get_by_id(root.id) if deleg.is_terminal else None

    if final is None:
        # 超时：deferred 回灌；严禁主人格对群报「还在跑/任务编号/等会儿」
        mark_deferred_main_delivery(root.id)
        fresh_after = await AIAgentTask.get_by_id(root.id)
        if fresh_after is not None and fresh_after.status in (
            "completed",
            "failed",
            "cancelled",
            "waiting_approval",
        ):
            if try_claim_deferred_for_inline_return(root.id):
                final = fresh_after
            else:
                return (
                    f"✅ 任务#{root.ordinal} 刚好完成，框架正在回灌产物。"
                    "请只输出 <SILENCE>，勿向用户说话、勿重复 create_subagent。"
                )
        else:
            # 给模型**能被工具消费**的单一句柄（INV-5）：旧版只印 8 字符前缀，
            # 而 list_persisted_outputs 是 SQL 等值查询 → 模型怎么查都是空。
            return (
                f"⏳ 子任务后台执行中（已同步等 {int(waited)}s，将自动回灌）。"
                f"task#{root.ordinal} / {pid} / 句柄 {delegation_handle(root.id)}\n"
                "本 tool_return 不是终局结论。"
                "对用户默认 <SILENCE>"
                "（禁止过程动词、任务编号、句柄、编排词、叙述第二个执行者）。"
                "禁止再 create_subagent 同任务。\n"
                "完成后自动回灌。用户之后追问进度时，用 find_tools 召回 check_delegation"
                "（句柄只进工具参数，绝不写进给用户看的台词）。"
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
            binary_tag = "（落盘文件/文本，文本类请 artifact_get 取原文再 render）"
        art_lines.append(f"  - {a.id} | {a.mime or 'text/plain'} | {a.summary[:80]}{binary_tag}")
        if not primary_handle and a.payload_path and (a.mime or "").startswith("image/"):
            primary_handle = a.id
    if not primary_handle and arts:
        primary_handle = arts[0].id

    _status_labels = {
        "completed": "✅ 已完成",
        "failed": "❌ 失败",
        "cancelled": "🚫 已取消",
        "waiting_approval": "⏸️ 等待审批",
    }
    status_label = _status_labels[final.status] if final.status in _status_labels else final.status

    from gsuid_core.ai_core.capability_agents.delegation_contracts import (
        receipt_image_likely,
    )

    has_image_art = any(bool(a.payload_path) and (a.mime or "").startswith("image/") for a in arts)
    parts = [
        f"【{pid} 代理完成 - Kanban 任务#{root.ordinal}】 {status_label}",
        f"任务: {root.display_name}",
        _main_persona_receipt_hint(image_likely=receipt_image_likely(pid=pid, has_image_art=has_image_art)),
        "文本类 res_ 请 artifact_get 取原文，**不要** read_image。",
    ]
    if final.failure_reason:
        parts.append(f"失败原因: {final.failure_reason[:300]}")
    if art_lines:
        parts.append("产物 artifact:")
        parts.extend(art_lines)
        if primary_handle:
            parts.append(
                f"💡 主要产物句柄: `{primary_handle}`"
                "（图片类 send_message_by_ai(image_id=)；文本类 artifact_get 后 render——"
                "**只在参数里用这个句柄，绝不要把 res_/img_ 句柄本身写进给用户看的话里**）"
            )
    else:
        parts.append("（本任务无显式 artifact 登记）")

    # 落盘 text/* 也要读出（大段 markdown 不在 payload_inline）
    from gsuid_core.ai_core.planning.kanban_executor import _artifact_text_excerpt

    text_excerpt = _artifact_text_excerpt(arts, limit=4000)
    if text_excerpt:
        parts.append(
            "\n⬇️ 下面是代理的结论，请你用角色口吻**转述给用户**（这不是给你自己看的备忘，"
            "用户还没看到；转述时不要提任何 res_/任务 id）：\n" + text_excerpt
        )
        if pid == "research_agent":
            note = _factpack_freshness_note(text_excerpt)
            if note:
                parts.append(note.strip())
    return "\n".join(parts)
