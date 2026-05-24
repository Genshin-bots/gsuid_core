"""能力代理运行器。

``run_capability_agent()``：按画像装配工具集，跑一个**无人格**的 Plan-and-Solve
Agent，返回其交付结果（纯文本）。

它是"被 Kanban 调度器派出执行子任务"的无状态执行体——任务树状态机由 ``kanban``
manager 承担。架构为 hub-and-spoke（星型）：人格编排层 + 多个专职执行者，不引入
点对点消息总线。

**ad-hoc workspace 保护**（2026-05-23 加固）：``create_subagent(agent_profile=...)``
路径直接调本函数，跳过 ``kanban_executor._run_one_task_node``，因此没有 ``PlanRunContext``
绑定。如果不补一层，``code_agent`` 的 ``write_file_content`` / ``execute_shell_command``
会落到 ``FILE_PATH`` 甚至项目根 cwd，污染主仓库。

本模块用 ``_ensure_adhoc_workspace`` 上下文管理器在入口判断：

- **已有 plan_ctx**（Kanban 派活路径）→ 透传现有 ctx，零开销。
- **没有 plan_ctx**（create_subagent 路径）→ 建一个 ad-hoc PlanRunContext：
  - ``root_task_id = "adhoc_<session_hash>"``、``task_id = "adhoc_<profile>_<ts>_<rand>"``
  - workspace = ``data/ai_core/artifacts/adhoc_<sess>/<task>/workspace/<profile>/``
  - 所有 file/shell 工具天然走 workspace 沙盒（无路径越界、cwd 永不落到项目根）
  - 命令前后扫描自动登记 ``workspace_file`` artifact（root_task_id 是 adhoc，
    artifact 表行仍可建——它没有 FK 到 AIAgentTask）。

ad-hoc artifact **不属于任何 Kanban 树**，仅供 webconsole / artifacts API 检索。
需要被主任务关联的产物仍必须走 ``register_kanban_task``。
"""

import time
import hashlib
from typing import List, Optional
from contextlib import asynccontextmanager

from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.rag.tools import ToolList

from .registry import CapabilityAgentProfile, get_profile

# 能力代理失败时返回字符串的统一前缀；调用方（kanban_executor）据此识别
# "代理跑挂了"——比正常的 "⚠️" 警示文本更长、更独特，避免 LLM 输出里偶发的
# 警示符号被误判为失败。任何修改都应同时检查所有引用点。
CAPABILITY_AGENT_ERROR_PREFIX = "⚠️ 能力代理执行失败"

# 能力代理永远附带的 Kanban 推进 / 基础工具。即便是即时委派也无害——不在任务
# 执行上下文时 artifact_put 会返回"当前不在 Kanban 任务上下文"而已。
_ALWAYS_TOOLS: List[str] = [
    "artifact_put",
    "artifact_get",
    "artifact_list",
    "state_set",
    "state_get",
    "state_append",
    "state_list",
    "search_knowledge",
    "web_search_tool",
    "web_fetch_tool",
]


def _resolve_tools(profile: CapabilityAgentProfile) -> ToolList:
    """按画像装配工具集：显式白名单 + 长任务推进/基础工具，按名从全局注册表取。"""
    from gsuid_core.ai_core.register import get_all_tools

    all_tools = get_all_tools()  # Dict[name, ToolBase]
    names: List[str] = list(dict.fromkeys(profile.tool_names + _ALWAYS_TOOLS))
    tools: ToolList = [all_tools[n].tool for n in names if n in all_tools]
    return tools


@asynccontextmanager
async def _ensure_adhoc_workspace(profile_id: str, ev: Optional[Event]):
    """若当前没绑 ``PlanRunContext``，建一个 ad-hoc workspace；否则透传现有 ctx。

    退出时：ad-hoc 创建的自动 reset；透传的不动用户原 ctx。

    设计动机：``create_subagent(agent_profile=...)`` 路径直接调 ``run_capability_agent``，
    跳过 Kanban executor。如果不补一层 workspace 绑定，code_agent 的 file/shell
    工具会落到 FILE_PATH / 项目根 cwd，污染主仓库（参见 plans/agent_kanban_workspace_fix_20260523.md §2）。

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
    rand_suffix = hashlib.md5(f"{ts}-{profile_id}".encode()).hexdigest()[:6]
    adhoc_root_id = f"adhoc_{sess_hash}"
    adhoc_task_id = f"adhoc_{profile_id}_{ts}_{rand_suffix}"

    try:
        # 注意：ensure_workspace 不再按 agent_profile 分子目录（见 workspace.py），
        # 这里传 profile_id 仅作历史兼容，实际只创建 workspace/。
        workspace = ensure_workspace(adhoc_root_id, adhoc_task_id, agent_profile=profile_id)
    except OSError as e:
        logger.error(f"🤖 [CapabilityAgent] 创建 ad-hoc workspace 失败: {e}；放弃绑定（落 FILE_PATH 兜底）")
        yield None
        return

    ctx = PlanRunContext(
        task_id=adhoc_task_id,
        step_id=None,
        root_task_id=adhoc_root_id,
        artifact_workspace=workspace,
        allowed_write_roots=[workspace],
        agent_profile=profile_id,
    )
    token = bind_plan_context(ctx)
    logger.info(
        f"🤖 [CapabilityAgent] 建立 ad-hoc workspace: {workspace} (adhoc_root={adhoc_root_id}, profile={profile_id})"
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
    """实例化一个能力代理并同步运行，返回其交付结果（纯文本）。

    - 无人格：system_prompt 来自画像，persona_name=None。
    - 工具：画像白名单 + 长任务推进工具（显式传入，绕过 _execute_run 的自动装配）。
    - return_mode="return"：文本不直接下发给用户；但工具内 bot.send（如 HITL
      审批通知）仍生效——这是"能力代理执行但不直接面向用户说话、HITL 通知却能
      送达"的依据。
    - **workspace 沙盒**：无论从 Kanban 派活还是 ``create_subagent`` 兜底，
      执行体一定有一个 ``PlanRunContext.artifact_workspace`` 绑定（见
      ``_ensure_adhoc_workspace`` 注释）；file / shell 工具不会落到项目根。
    """
    from gsuid_core.ai_core.gs_agent import create_agent

    profile = get_profile(profile_id)
    if profile is None:
        return f"⚠️ 能力代理画像不存在: {profile_id}"

    tools = _resolve_tools(profile)
    # research/code 画像 tool_names 为空时，按 tool_query 或 task 再补一轮向量检索
    if profile.tool_query or not profile.tool_names:
        try:
            from gsuid_core.ai_core.rag.tools import search_tools

            extra = await search_tools(
                query=profile.tool_query or task,
                limit=8,
                non_category="self",
            )
            seen = {t.name for t in tools}
            tools += [t for t in extra if t.name not in seen]
        except Exception as e:
            logger.debug(f"🤖 [CapabilityAgent] 工具检索失败: {e}")

    session_id = f"capagent_{profile_id}_{session_id_suffix or 'adhoc'}"

    # ad-hoc workspace 包住整段 agent.run——Kanban 路径下是透传，开销忽略。
    async with _ensure_adhoc_workspace(profile_id, ev) as plan_ctx:
        ws_label = plan_ctx.artifact_workspace if plan_ctx else "FILE_PATH-fallback"
        agent = create_agent(
            system_prompt=profile.system_prompt,
            max_tokens=profile.max_tokens,
            max_iterations=profile.max_iterations,
            create_by="CapabilityAgent",  # 不在工具自动装配白名单，配合显式 tools 跳过装配
            task_level="high",
            session_id=session_id,
            is_subagent=True,
        )
        logger.info(
            f"🤖 [CapabilityAgent] 启动「{profile.display_name}」({profile_id})，"
            f"工具 {len(tools)} 个，workspace={ws_label}，任务: {task[:50]}..."
        )
        try:
            result = await agent.run(
                user_message=task,
                bot=bot,
                ev=ev,
                tools=tools,  # 显式传入 → 跳过自动装配
                return_mode="return",  # 文本不下发，作为返回值交回
            )
            return str(result)
        except Exception as e:
            logger.error(f"🤖 [CapabilityAgent] 「{profile_id}」执行失败: {e}")
            return f"{CAPABILITY_AGENT_ERROR_PREFIX}: {e}"
        finally:
            session_logger = agent._session_logger
            if session_logger is not None:
                session_logger.close()
