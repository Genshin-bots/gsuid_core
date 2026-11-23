"""能力代理运行器（AgentNode task-mode 实例化）。

``run_capability_agent()``：按节点装配工具集（tool_packs + tool_names），跑一个
Plan-and-Solve Agent，返回其交付结果（纯文本）。系统提示词 = 节点身份核 +
交付边界，再附上委派人的人格 ID。需要时再读 persona.md。不抄主会话的框架约束。

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
from typing import TYPE_CHECKING, List, Optional
from contextlib import asynccontextmanager

from gsuid_core.bot import Bot
from gsuid_core.i18n import t as i18n_t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.rag.tools import ToolList
from gsuid_core.ai_core.agent_node import (
    TASK_BASICS_PACK,
    AgentNode,
    get_node,
    has_dynamic_pack,
    compose_task_prompt,
    resolve_pack_tool_names,
)
from gsuid_core.ai_core.configs.task_kinds import capability_agent_task_kind

if TYPE_CHECKING:
    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent

# 能力代理失败时返回字符串的统一前缀；调用方（kanban_executor）据此识别
# "代理跑挂了"。任何修改都应同时检查所有引用点。
CAPABILITY_AGENT_ERROR_PREFIX = "⚠️ 能力代理执行失败"

# 非 render 能力代理：task 向量回填/族展开也不得再拿到出图与嵌套委派入口。
# 出图主权：主人格 → create_subagent(render_agent)；业务节点只交事实包。
_NON_RENDER_CAP_DENY_TOOLS = frozenset(
    {
        "create_subagent",
        "check_delegation",
        "register_kanban_task",
        "evaluate_agent_mesh_capability",
        "respawn_subtask",
        "fail_task_tree",
        "pause_my_kanban_tree",
        "resume_my_kanban_tree",
        "disarm_my_recurring_template",
        "send_message_by_ai",
        "render_html_to_image",
        "render_card",
        "render_markdown_to_image",
        "find_images",
    }
)


_CAP_PLUMBING_TOOLS = frozenset(
    {
        "artifact_put",
        "artifact_get",
        "artifact_list",
        "state_get",
        "state_list",
        "read_handle",
        "list_persisted_outputs",
        "get_self_persona_info",
        "find_tools",
        "call_tool",
        "run_code",
    }
)


def _node_primary_tools_all_failed(
    node: AgentNode,
    outcomes: tuple[frozenset[str], frozenset[str]],
) -> bool:
    """节点声明的专属业务工具（无专属声明则取非基建工具）被调用过但 0 成功。"""
    ok_set, err_set = outcomes
    if not err_set:
        return False
    domain_packs = [p for p in node.tool_packs if p != TASK_BASICS_PACK]
    owned = (set(resolve_pack_tool_names(domain_packs)) | set(node.tool_names)) - _CAP_PLUMBING_TOOLS
    target = owned if owned else ((ok_set | err_set) - _CAP_PLUMBING_TOOLS)
    return bool(err_set & target) and not bool(ok_set & target)


def _resolve_tools(node: AgentNode) -> ToolList:
    """按节点装配工具集：能力族（静态 packs）+ 显式白名单，按名从全局注册表取。"""
    from gsuid_core.ai_core.register import get_all_tools

    all_tools = get_all_tools()  # Dict[name, ToolBase]
    names: List[str] = list(dict.fromkeys(resolve_pack_tool_names(node.tool_packs) + node.tool_names))
    if "get_self_persona_info" not in names:
        names.append("get_self_persona_info")
    tools: ToolList = [all_tools[n].tool for n in names if n in all_tools]
    return tools


def _strip_non_render_cap_deny(tools: ToolList, *, node_id: str) -> ToolList:
    """render_agent 保留渲染白名单；其它能力代理剥离嵌套委派与 render_*。"""
    if node_id == "render_agent":
        from gsuid_core.ai_core.configs.ai_config import render_embed_images_enabled

        if render_embed_images_enabled():
            return tools
        return [t for t in tools if t.name != "find_images"]
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


def capability_tools(profile_id: str, ev: Optional[Event]) -> ToolList:
    """节点白名单。续作不再做一次任务向量回填，避免换一套工具名。"""
    node = get_node(profile_id)
    if node is None:
        return []
    tools = _strip_non_render_cap_deny(_resolve_tools(node), node_id=node.node_id)
    from gsuid_core.ai_core.tool_risk import strip_high_risk_tools

    return strip_high_risk_tools(tools, ev)


def format_delegator_persona_ref(persona_name: str) -> str:
    """系统提示词只放人格 ID。角色卡由工具按需读取。"""
    name = persona_name.strip()
    if not name:
        return ""
    return (
        "【委派你的人格】\n"
        f"人格 ID：{name}\n"
        "大多数任务不要读角色卡。只有要替这个人做选择、填表、答题时，"
        f'调用 get_self_persona_info(info_type="card", persona_name="{name}") 读取 persona.md。\n'
        "读到的性格只用于这些选择。工具和交付仍按本提示词前面的说明。不要用角色卡对用户说话。"
    )


def delegator_persona_ref(persona_name: str) -> str:
    """人格资料不存在时不写 ID。"""
    name = persona_name.strip()
    if not name:
        return ""
    from gsuid_core.ai_core.persona.persona import Persona

    if name != "智能助手" and not Persona(name).exists():
        return ""
    return format_delegator_persona_ref(name)


def delegator_persona_name(ev: Optional[Event], explicit: str = "") -> str:
    """显式名优先，否则从父会话或会话路由取当前人格。"""
    name = explicit.strip()
    if name:
        return name
    if ev is None or not ev.session_id:
        return ""
    sid = str(ev.session_id)
    from gsuid_core.ai_core.session_registry import get_ai_session_registry

    parent = get_ai_session_registry().get_ai_session(sid)
    if parent is not None and isinstance(parent.persona_name, str) and parent.persona_name.strip():
        return parent.persona_name.strip()
    from gsuid_core.ai_core.persona.config import persona_config_manager

    found = persona_config_manager.get_persona_for_session(sid)
    if isinstance(found, str) and found.strip():
        return found.strip()
    return ""


async def task_system_prompt(node: AgentNode, ev: Optional[Event], persona_name: str = "") -> str:
    """任务说明后面只附人格 ID。没有人格时只留任务说明。"""
    base = compose_task_prompt(node)
    block = delegator_persona_ref(delegator_persona_name(ev, persona_name))
    if not block:
        return base
    return f"{base.rstrip()}\n\n{block}"


async def _build_capability_session(
    node: AgentNode,
    session_id: str,
    ev: Optional[Event],
    persona_name: str = "",
) -> "GsCoreAIAgent":
    """构建能力代理实例并注册到会话表与父会话日志。"""
    from gsuid_core.ai_core.gs_agent import create_agent
    from gsuid_core.ai_core.session_registry import get_ai_session_registry
    from gsuid_core.ai_core.configs.ai_config import ai_config

    agent = create_agent(
        system_prompt=await task_system_prompt(node, ev, persona_name),
        max_tokens=ai_config.get_config("task_max_tokens").data,
        create_by="CapabilityAgent",
        task_kind=capability_agent_task_kind(node.node_id),
        task_level="high",
        session_id=session_id,
        is_subagent=True,
        dynamic_tools=True if has_dynamic_pack(node.tool_packs) else None,
        wall_clock_budget=420.0,
        capability_node_id=node.node_id,
    )
    get_ai_session_registry().set_ai_session(session_id, agent)
    _link_capability_loggers(ev, agent, session_id)
    return agent


async def reopen_capability_agent(profile_id: str, session_id: str, ev: Optional[Event]) -> Optional["GsCoreAIAgent"]:
    """进程重启后用同一个 session_id 拉起。不发送原来的任务书。"""
    node = get_node(profile_id)
    if node is None:
        return None
    return await _build_capability_session(node, session_id, ev)


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
    persona_name: str = "",
) -> str:
    """按 node_id 实例化一个 task-mode 节点并同步运行，返回其交付结果（纯文本）。

    - 系统提示词 = 节点身份核 + 交付边界 + 委派人人格 ID。不抄主会话框架约束。
    - 工具：packs + 白名单为保底；**始终**再按 task（及可选 tool_query）向量检索
      增补专业工具并做能力族展开。节点声明 ``dynamic`` 族时 gs_agent 逐轮
      五层装配并与显式工具合并。
    - return_mode="return"：文本不直接下发给用户；工具内 bot.send（如 HITL
      审批通知）仍生效。
    - **workspace 沙盒**：执行体一定有 ``PlanRunContext.artifact_workspace`` 绑定。
    """
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
                from gsuid_core.ai_core.register import is_core_only_category

                seeds = [
                    t for t in seeds if t.name not in _NON_RENDER_CAP_DENY_TOOLS and not is_core_only_category(t.name)
                ]
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
    # 非主人即使绕过节点门，装配结果里也不留高危执行工具。
    from gsuid_core.ai_core.tool_risk import strip_high_risk_tools

    tools = strip_high_risk_tools(tools, ev)

    session_id = f"capagent_{node.node_id}_{session_id_suffix or 'adhoc'}"

    # ad-hoc workspace 包住整段 agent.run——Kanban 路径下是透传，开销忽略。
    async with _ensure_adhoc_workspace(node.node_id, ev) as plan_ctx:
        ws_label = plan_ctx.artifact_workspace if plan_ctx else "FILE_PATH-fallback"
        agent = await _build_capability_session(node, session_id, ev, persona_name)
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
            result = await agent.run(
                user_message=task,
                bot=bot,
                ev=ev,
                tools=tools,
                return_mode="return",
            )
            out_str = str(result)
            if not out_str.lstrip().startswith(CAPABILITY_AGENT_ERROR_PREFIX) and _node_primary_tools_all_failed(
                node, agent.last_run_tool_outcomes
            ):
                has_real_file = False
                if plan_ctx is not None and plan_ctx.task_id:
                    from gsuid_core.ai_core.planning.models import AIAgentArtifact

                    arts = await AIAgentArtifact.list_for_task(plan_ctx.task_id)
                    has_real_file = any(bool(a.payload_path) and not (a.mime or "").startswith("text/") for a in arts)
                if not has_real_file:
                    summary = " ".join(out_str.split())[:180] or "业务工具调用均报错"
                    return f"{CAPABILITY_AGENT_ERROR_PREFIX}: {summary}"
            return out_str
        except Exception as e:
            logger.error(i18n_t("log.ai.cap_agent_fail_execution_failed", p0=node.node_id, e=e))
            return f"{CAPABILITY_AGENT_ERROR_PREFIX}: {e}"
        finally:
            from gsuid_core.ai_core.planning.runtime import get_plan_context

            plan_ctx = get_plan_context()
            keep = False
            if plan_ctx is not None and plan_ctx.task_id:
                from gsuid_core.ai_core.planning.models import AIAgentTask

                held = await AIAgentTask.get_by_id(plan_ctx.task_id)
                keep = held is not None and held.status == "waiting_approval"
            # 挂起后还要 session.run 续作，日志和注册表都留着。
            if not keep:
                session_logger = agent._session_logger
                if session_logger is not None:
                    session_logger.close()
