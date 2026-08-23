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

# 非 render 能力代理：task 向量回填/族展开也不得再拿到出图与嵌套委派入口。
# 出图主权：主人格 → create_subagent(render_agent)；业务节点只交事实包。
_NON_RENDER_CAP_DENY_TOOLS = frozenset(
    {
        "create_subagent",
        "render_html_to_image",
        "render_card",
        "render_markdown_to_image",
    }
)


def _resolve_tools(node: AgentNode) -> ToolList:
    """按节点装配工具集：能力族（静态 packs）+ 显式白名单，按名从全局注册表取。"""
    from gsuid_core.ai_core.register import get_all_tools

    all_tools = get_all_tools()  # Dict[name, ToolBase]
    names: List[str] = list(dict.fromkeys(resolve_pack_tool_names(node.tool_packs) + node.tool_names))
    tools: ToolList = [all_tools[n].tool for n in names if n in all_tools]
    return tools


def _strip_non_render_cap_deny(tools: ToolList, *, node_id: str) -> ToolList:
    """render_agent 保留渲染白名单；其它能力代理剥离嵌套委派与 render_*。"""
    if node_id == "render_agent":
        return tools
    kept = [t for t in tools if t.name not in _NON_RENDER_CAP_DENY_TOOLS]
    if len(kept) != len(tools):
        stripped = sorted({t.name for t in tools} - {t.name for t in kept})
        logger.info(
            i18n_t(
                "log.ai.cap_stripped_non_render_deny",
                node_id=node_id,
                names=stripped,
            )
        )
    return kept


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
        logger.error(i18n_t("log.ai.cap_create_ad_hoc_workspace", e=e))
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
            "log.ai.cap_ad_hoc_workspace_established",
            workspace=workspace,
            adhoc_root_id=adhoc_root_id,
            node_id=node_id,
        )
    )
    try:
        yield ctx
    finally:
        reset_plan_context(token)


def _link_capability_loggers(ev: Optional[Event], agent: object, child_session_id: str) -> None:
    """能力代理与父 session 双向 link_agent。"""
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    if ev is None or not ev.session_id:
        return
    if not isinstance(agent, GsCoreAIAgent):
        return
    parent = get_ai_session_registry().get_ai_session(ev.session_id)
    if parent is None:
        return
    parent_logger = parent._session_logger
    sub_logger = agent._session_logger
    if parent_logger is None or sub_logger is None:
        return
    parent_logger.link_agent(
        agent_session_id=child_session_id,
        agent_session_uuid=sub_logger.session_uuid,
        agent_type="sub_agent",
        persona_name=agent.persona_name,
        create_by=agent.create_by,
        log_file=str(sub_logger._file_path),
    )
    sub_logger.link_agent(
        agent_session_id=ev.session_id,
        agent_session_uuid=parent_logger.session_uuid,
        agent_type="parent_agent",
        persona_name=parent.persona_name,
        create_by=parent.create_by,
        log_file=str(parent_logger._file_path),
    )


async def run_capability_agent(
    profile_id: str,
    task: str,
    ev: Optional[Event],
    bot: Optional[Bot] = None,
    session_id_suffix: str = "",
) -> str:
    """按 node_id 实例化一个 task-mode 节点并同步运行，返回其交付结果（纯文本）。

    - 系统提示词 = 节点身份核 + 交付边界叠加层（persona_name=None，无人格）。
    - 工具：packs + 白名单为保底；**始终**再按 task（及可选 tool_query）向量检索
      增补专业工具并做能力族展开。节点声明 ``dynamic`` 族时 gs_agent 逐轮
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
    # render_agent：只吃白名单渲染工具，禁止 task 向量回填把 web_search 捞进来。
    # 其余节点：packs + tool_names 为保底，再按 task 检索增补专业工具。
    if node.node_id != "render_agent":
        try:
            from gsuid_core.ai_core.rag.tools import search_tools, expand_tools_to_families

            tq = (node.tool_query or "").strip()
            task_text = (task or "").strip()
            if tq and task_text:
                search_query = f"{tq}\n{task_text}"
            else:
                search_query = tq or task_text

            if search_query:
                recall = int(ai_config.get_config("tool_search_recall").data or 8)
                max_extra = int(ai_config.get_config("tool_extra_pool_max").data or 8)
                seeds = await search_tools(
                    query=search_query,
                    limit=max(recall, 8),
                    non_category="self",
                )
                # 种子与族展开后都会再 strip：避免整族带回 create_subagent/render_*
                seeds = [t for t in seeds if t.name not in _NON_RENDER_CAP_DENY_TOOLS]
                seen = {t.name for t in tools}
                extra = expand_tools_to_families(
                    seeds,
                    exclude_names=seen | set(_NON_RENDER_CAP_DENY_TOOLS),
                    max_tools=max_extra,
                )
                if extra:
                    tools = tools + extra
                    logger.info(
                        i18n_t(
                            "log.ai.cap_task_backfill_query_names",
                            n=len(extra),
                            q=search_query[:60],
                            names=[t.name for t in extra][:12],
                        )
                    )
        except Exception as e:
            logger.debug(i18n_t("log.ai.cap_retrieval", e=e))

    tools = _strip_non_render_cap_deny(tools, node_id=node.node_id)

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
            wall_clock_budget=420.0,  # 留 80s 余量给外层 500s 硬超时，420s 时注入收敛提示
            capability_node_id=node.node_id,
        )
        logger.info(
            i18n_t(
                "log.ai.cap_tools_workspace_ws_label",
                p0=node.display_name,
                p1=node.node_id,
                p2=len(tools),
                ws_label=ws_label,
                p3=task[:50],
            )
        )
        try:
            from gsuid_core.ai_core.session_registry import get_ai_session_registry

            get_ai_session_registry().set_ai_session(session_id, agent)
            _link_capability_loggers(ev, agent, session_id)
            result = await agent.run(
                user_message=task,
                bot=bot,
                ev=ev,
                tools=tools,  # 显式传入；dynamic 节点由 gs_agent 合并五层装配
                return_mode="return",  # 文本不下发，作为返回值交回
            )
            return str(result)
        except Exception as e:
            logger.error(i18n_t("log.ai.cap_agent_fail_execution_failed", p0=node.node_id, e=e))
            return f"{CAPABILITY_AGENT_ERROR_PREFIX}: {e}"
        finally:
            session_logger = agent._session_logger
            if session_logger is not None:
                session_logger.close()
