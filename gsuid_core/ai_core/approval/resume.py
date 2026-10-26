"""批准后把续作卡回灌进提交时的那个 agent。不调用 run_capability_agent。"""

import re
from typing import TYPE_CHECKING, Optional

from gsuid_core.bot import Bot
from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.control.lines import approval_resume_line
from gsuid_core.ai_core.session_registry import get_ai_session_registry

from .halt import action_failed, text_is_approval_halt
from .models import AIApprovalRequest

if TYPE_CHECKING:
    from contextvars import Token

    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.planning.runtime import PlanRunContext

_RES = re.compile(r"res_[0-9a-fA-F]{3,}")


def _payload_str(raw: str, key: str) -> str:
    import json

    data = json.loads(raw or "{}")
    if not isinstance(data, dict) or key not in data:
        return ""
    val = data[key]
    if isinstance(val, str):
        return val
    return ""


def _event_for(row: AIApprovalRequest) -> Event:
    user_type = row.user_type if row.user_type in ("group", "direct", "channel", "sub_channel") else "direct"
    return Event(
        bot_id=row.bot_id,
        user_id=row.operator_user_id,
        bot_self_id=row.bot_self_id,
        user_type=user_type,
        group_id=row.group_id,
        real_bot_id=row.bot_id,
        msg_id="",
        user_pm=row.operator_user_pm,
        WS_BOT_ID=_payload_str(row.payload_json, "ws_bot_id"),
    )


async def _agent_for(row: AIApprovalRequest, ev: Event) -> Optional["GsCoreAIAgent"]:
    registry = get_ai_session_registry()
    session_id = (row.origin_session_id or "").strip()
    if not session_id:
        return None
    agent = registry.get_ai_session(session_id)
    if agent is not None:
        return agent
    if not session_id.startswith("capagent_"):
        return None
    profile = _payload_str(row.payload_json, "agent_profile")
    if not profile and row.ref_key:
        from gsuid_core.ai_core.planning.models import AIAgentTask

        task = await AIAgentTask.get_by_id(row.ref_key)
        if task is not None and task.agent_profile:
            profile = task.agent_profile
    if not profile:
        return None
    from gsuid_core.ai_core.capability_agents.runner import reopen_capability_agent

    return await reopen_capability_agent(profile, session_id, ev)


async def reinject_approved(row: AIApprovalRequest, result_text: str) -> None:
    """同一 agent 的下一次 run。迭代预算是这次 run 自己的，不接上次用尽的计数。"""
    ev = _event_for(row)
    agent = await _agent_for(row, ev)
    if agent is None:
        logger.warning(t("log.ai.approval_resume_missing_session", p0=row.short_id, p1=row.origin_session_id))
        await _fail_task(row, "原来的会话不在了")
        from .notify import notify_parent_group

        await notify_parent_group(row, (result_text or "已批准，原来的会话不在了。")[:800])
        return
    card = await _card(row, result_text)
    from gsuid_core.gss import gss

    ws = _payload_str(row.payload_json, "ws_bot_id")
    raw_bot = gss.active_bot[ws] if ws in gss.active_bot else None
    if raw_bot is None and row.bot_id in gss.active_bot:
        raw_bot = gss.active_bot[row.bot_id]
    bot = Bot(raw_bot, ev) if raw_bot is not None else None
    return_mode = "return" if (row.origin_session_id or "").startswith("capagent_") else "by_bot"
    tools = None
    if return_mode == "return":
        profile = _payload_str(row.payload_json, "agent_profile")
        if profile:
            from gsuid_core.ai_core.capability_agents.runner import capability_tools

            tools = capability_tools(profile, ev)
    token = await _bind_task(row)
    try:
        try:
            raw = await agent.run(
                user_message=card,
                bot=bot,
                ev=ev,
                tools=tools,
                return_mode=return_mode,
                is_framework_injection=True,
            )
        except Exception as e:
            # 审批行已经落成批准。这里把关联任务标失败，避免停在 running。
            logger.exception(t("log.ai.approval_resume_fail", p0=row.short_id, e=e))
            await _fail_task(row, f"{type(e).__name__}: {e}")
            return
    finally:
        if token is not None:
            from gsuid_core.ai_core.planning.runtime import reset_plan_context

            reset_plan_context(token)
    if return_mode == "return":
        await _settle_capability(row, str(raw), bot)
    logger.info(t("log.ai.approval_resume_ok", p0=row.short_id))


async def _card(row: AIApprovalRequest, result_text: str) -> str:
    task_id = row.ref_key or row.request_id
    ordinal = 0
    display = row.category
    goal = row.title
    if row.category == "kanban_subtask" and row.ref_key:
        from gsuid_core.ai_core.planning.models import AIAgentTask

        task = await AIAgentTask.get_by_id(row.ref_key)
        if task is not None:
            task_id = task.id
            ordinal = task.ordinal
            display = task.display_name
            goal = task.goal or task.display_name
    handles = _RES.findall(result_text)
    handle_line = handles[0] if handles else "无句柄，正文如下"
    phase = "installed" if "已安装" in result_text else "已执行"
    if row.category == "kanban_subtask" and "插件" in (row.effect or row.title):
        next_step = "加载，然后自测"
    else:
        next_step = "按上面的结果继续，不要重做刚批准的那一步。"
    return approval_resume_line(
        task_id=task_id,
        ordinal=ordinal,
        display_name=display,
        goal_line=goal,
        short_id=row.short_id,
        effect=row.effect or row.title[:120],
        happened=result_text or "已批准。",
        phase=phase,
        next_step=next_step,
        handle_line=handle_line,
    )


async def _bind_task(row: AIApprovalRequest) -> "Token[PlanRunContext | None] | None":
    if row.category != "kanban_subtask" or not row.ref_key:
        return None
    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.ai_core.planning.runtime import PlanRunContext, bind_plan_context
    from gsuid_core.ai_core.planning.workspace import ensure_workspace

    task = await AIAgentTask.get_by_id(row.ref_key)
    if task is None:
        return None
    if task.status == "waiting_approval":
        await AIAgentTask.update_data_by_data(
            select_data={"id": task.id},
            update_data={"status": "running"},
        )
    root_id = task.root_task_id or task.id
    workspace = ensure_workspace(root_id, task.id, agent_profile=task.agent_profile or "")
    ctx = PlanRunContext(
        task_id=task.id,
        step_id=None,
        root_task_id=root_id,
        artifact_workspace=workspace,
        allowed_write_roots=[workspace],
        agent_profile=task.agent_profile or "",
    )
    return bind_plan_context(ctx)


async def _fail_task(row: AIApprovalRequest, reason: str) -> None:
    if row.category != "kanban_subtask" or not row.ref_key:
        return
    from gsuid_core.ai_core.planning import kanban
    from gsuid_core.ai_core.planning.models import AIAgentTask

    task = await AIAgentTask.get_by_id(row.ref_key)
    if task is None:
        return
    await kanban.mark_subtask_failed(task, reason[:1000])


async def _pause_task(task_id: str) -> None:
    from gsuid_core.ai_core.planning.models import AIAgentTask

    await AIAgentTask.update_data_by_data(
        select_data={"id": task_id},
        update_data={"status": "waiting_approval"},
    )


async def _settle_capability(row: AIApprovalRequest, raw: str, bot: Optional[Bot]) -> None:
    if row.category != "kanban_subtask" or not row.ref_key:
        return
    from gsuid_core.ai_core.planning import kanban
    from gsuid_core.ai_core.planning.models import AIAgentTask
    from gsuid_core.ai_core.approval.session_ctx import session_has_pending
    from gsuid_core.ai_core.planning.kanban_executor import _finish_capability_delivery

    task = await AIAgentTask.get_by_id(row.ref_key)
    if task is None or task.status in ("waiting_approval", "failed", "completed"):
        return
    text = raw.strip()
    if text_is_approval_halt(text) or session_has_pending(row.origin_session_id):
        await _pause_task(task.id)
        return
    root = await AIAgentTask.get_by_id(task.root_task_id) if task.root_task_id else task
    if root is None:
        root = task
    if not text or action_failed(text):
        reason = text or "续作没有产出"
        await kanban.mark_subtask_failed(task, reason[:1000])
        await _finish_capability_delivery(root=root, child=task, raw_result=reason, bot=bot, is_failure=True)
        return
    await kanban.mark_subtask_completed(task)
    await _finish_capability_delivery(root=root, child=task, raw_result=text, bot=bot)
