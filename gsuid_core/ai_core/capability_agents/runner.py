"""能力代理运行器（AgentNode task-mode 实例化）。

``run_capability_agent()``：按节点装配工具集（tool_packs + tool_names），跑一个
**无人格**的 Plan-and-Solve Agent，返回其交付结果（纯文本）。系统提示词 =
节点身份核 + 交付边界叠加层（``compose_task_prompt``，节点可 boundary_override）。

预算：max_iterations / max_tokens 统一读全局配置 ``task_max_iterations`` /
``task_max_tokens``；Token 消耗经 gs_agent 预算 scope（ev 派生 / contextvar 继承）
上溯到来源会话记账。

它是"被 Kanban 调度器派出执行子任务"的无状态执行体——任务树状态机由 ``kanban``
manager 承担。架构为 hub-and-spoke（星型）：人格编排层 + 多个专职执行者，不引入
点对点消息总线。

**ad-hoc workspace 保护**：``create_subagent(agent_profile=...)`` 路径直接调本函数，
跳过 ``kanban_executor._run_one_task_node``，因此没有 ``PlanRunContext`` 绑定。
``_ensure_adhoc_workspace`` 在入口判断：

- **已有 plan_ctx**（Kanban 派活路径）→ 透传现有 ctx，零开销。
- **没有 plan_ctx**（create_subagent 路径）→ 建一个 ad-hoc PlanRunContext，
  file/shell 工具天然走 workspace 沙盒（无路径越界、cwd 永不落到项目根）。

ad-hoc artifact **不属于任何 Kanban 树**，仅供 webconsole / artifacts API 检索。
"""

import time
import hashlib
from typing import List, Optional
from contextlib import asynccontextmanager

from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.rag.tools import ToolList
from gsuid_core.ai_core.agent_node import (
    AgentNode,
    get_node,
    has_dynamic_pack,
    compose_task_prompt,
    resolve_pack_tool_names,
)

# 能力代理失败时返回字符串的统一前缀；调用方（kanban_executor）据此识别
# "代理跑挂了"。任何修改都应同时检查所有引用点。
CAPABILITY_AGENT_ERROR_PREFIX = "⚠️ 能力代理执行失败"


def _resolve_tools(node: AgentNode) -> ToolList:
    """按节点装配工具集：能力族（静态 packs）+ 显式白名单，按名从全局注册表取。"""
    from gsuid_core.ai_core.register import get_all_tools

    all_tools = get_all_tools()  # Dict[name, ToolBase]
    names: List[str] = list(dict.fromkeys(resolve_pack_tool_names(node.tool_packs) + node.tool_names))
    tools: ToolList = [all_tools[n].tool for n in names if n in all_tools]
    return tools


@asynccontextmanager
async def _ensure_adhoc_workspace(node_id: str, ev: Optional[Event]):
    """若当前没绑 ``PlanRunContext``，建一个 ad-hoc workspace；否则透传现有 ctx。

    退出时：ad-hoc 创建的自动 reset；透传的不动用户原 ctx。
    yield 的 ctx 是 ``PlanRunContext`` 或 None（极早期 planning 模块未就绪时）。
    """
    from gsuid_core.ai_core.planning.runtime import (
        PlanRunContext,
        get_plan_context,
        bind_plan_context,
        reset_plan_context,
    )
    from gsuid_core.ai_core.planning.workspace import ensure_workspace

    existing = get_plan_context()
    if existing is not None and existing.artifact_workspace is not None:
        # Kanban 派活路径，零开销透传
        yield existing
        return

    # ad-hoc 路径：建临时 workspace
    sess_hash = "anon"
    if ev is not None and ev.session_id:
        sess_hash = hashlib.md5(ev.session_id.encode()).hexdigest()[:10]
    ts = int(time.time())
    rand_suffix = hashlib.md5(f"{ts}-{node_id}".encode()).hexdigest()[:6]
    adhoc_root_id = f"adhoc_{sess_hash}"
    adhoc_task_id = f"adhoc_{node_id}_{ts}_{rand_suffix}"

    try:
        # ensure_workspace 不再按 agent_profile 分子目录，传 node_id 仅作历史兼容
        workspace = ensure_workspace(adhoc_root_id, adhoc_task_id, agent_profile=node_id)
    except OSError as e:
        logger.error(i18n_t("🤖 [CapabilityAgent] 创建 ad-hoc workspace 失败: {e}；放弃绑定（落 FILE_PATH 兜底）", e=e))
        yield None
        return

    ctx = PlanRunContext(
        task_id=adhoc_task_id,
        step_id=None,
        root_task_id=adhoc_root_id,
        artifact_workspace=workspace,
        allowed_write_roots=[workspace],
        agent_profile=node_id,
    )
    token = bind_plan_context(ctx)
    logger.info(
        i18n_t(
            "🤖 [CapabilityAgent] 建立 ad-hoc workspace: {workspace} (adhoc_root={adhoc_root_id}, node={node_id})",
            workspace=workspace,
            adhoc_root_id=adhoc_root_id,
            node_id=node_id,
        )
    )
    try:
        yield ctx
    finally:
        reset_plan_context(token)


async def run_capability_agent(
    profile_id: str,
    task: str,
    ev: Optional[Event],
    bot: Optional[Bot] = None,
    session_id_suffix: str = "",
) -> str:
    """按 node_id 实例化一个 task-mode 节点并同步运行，返回其交付结果（纯文本）。

    - 系统提示词 = 节点身份核 + 交付边界叠加层（persona_name=None，无人格）。
    - 工具：packs + 白名单显式传入；节点声明 ``dynamic`` 族时 gs_agent 逐轮
      五层装配并与显式工具合并。
    - return_mode="return"：文本不直接下发给用户；工具内 bot.send（如 HITL
      审批通知）仍生效。
    - **workspace 沙盒**：执行体一定有 ``PlanRunContext.artifact_workspace`` 绑定。
    """
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.configs.ai_config import ai_config

    node = get_node(profile_id)
    if node is None:
        return f"⚠️ 能力代理节点不存在: {profile_id}"

    tools = _resolve_tools(node)
    # tool_names 为空 / 声明了 tool_query 时，按 tool_query 或 task 再补一轮向量检索
    if node.tool_query or not node.tool_names:
        try:
            from gsuid_core.ai_core.rag.tools import search_tools

            extra = await search_tools(
                query=node.tool_query or task,
                limit=8,
                non_category="self",
            )
            seen = {t.name for t in tools}
            tools += [t for t in extra if t.name not in seen]
        except Exception as e:
            logger.debug(i18n_t("🤖 [CapabilityAgent] 工具检索失败: {e}", e=e))

    session_id = f"capagent_{node.node_id}_{session_id_suffix or 'adhoc'}"

    # ad-hoc workspace 包住整段 agent.run——Kanban 路径下是透传，开销忽略。
    async with _ensure_adhoc_workspace(node.node_id, ev) as plan_ctx:
        ws_label = plan_ctx.artifact_workspace if plan_ctx else "FILE_PATH-fallback"
        agent = create_agent(
            system_prompt=compose_task_prompt(node),
            max_tokens=ai_config.get_config("task_max_tokens").data,
            max_iterations=ai_config.get_config("task_max_iterations").data,
            create_by="CapabilityAgent",  # 不在工具自动装配白名单，配合显式 tools
            task_level="high",
            session_id=session_id,
            is_subagent=True,
            dynamic_tools=True if has_dynamic_pack(node.tool_packs) else None,
        )
        logger.info(
            i18n_t(
                "🤖 [CapabilityAgent] 启动「{p0}」({p1})，工具 {p2} 个，workspace={ws_label}，任务: {p3}...",
                p0=node.display_name,
                p1=node.node_id,
                p2=len(tools),
                ws_label=ws_label,
                p3=task[:50],
            )
        )
        try:
            result = await agent.run(
                user_message=task,
                bot=bot,
                ev=ev,
                tools=tools,  # 显式传入；dynamic 节点由 gs_agent 合并五层装配
                return_mode="return",  # 文本不下发，作为返回值交回
            )
            return str(result)
        except Exception as e:
            logger.error(i18n_t("🤖 [CapabilityAgent] 「{p0}」执行失败: {e}", p0=node.node_id, e=e))
            return f"{CAPABILITY_AGENT_ERROR_PREFIX}: {e}"
        finally:
            session_logger = agent._session_logger
            if session_logger is not None:
                session_logger.close()
