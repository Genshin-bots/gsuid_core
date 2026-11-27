"""审批通知与批准后续作回灌（私聊确认码 / 关单通知 / 回灌原会话）。"""

from __future__ import annotations

import re
import json
import time
from typing import TYPE_CHECKING, Optional

from gsuid_core.bot import Bot, _Bot
from gsuid_core.i18n import t
from gsuid_core.config import core_config
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.control.lines import approval_resume_line
from gsuid_core.ai_core.session_registry import get_ai_session_registry

from .models import (
    DEFAULT_APPROVAL_TTL,
    AIApprovalRequest,
    action_failed,
    session_has_pending,
    text_is_approval_halt,
)

if TYPE_CHECKING:
    from contextvars import Token

    from gsuid_core.ai_core.gs_agent import GsCoreAIAgent
    from gsuid_core.ai_core.planning.runtime import PlanRunContext

WAITING_GROUP_TEXT = ""
_RES = re.compile(r"res_[0-9a-fA-F]{3,}")


def _payload_str(raw: str, key: str) -> str:
    data: object = json.loads(raw or "{}")
    if not isinstance(data, dict) or key not in data:
        return ""
    val = data[key]
    return val if isinstance(val, str) else ""


def _window_phrase(seconds: int) -> str:
    left = seconds if seconds > 0 else DEFAULT_APPROVAL_TTL
    if left % 60 == 0:
        return f"{left // 60} 分钟"
    if left >= 60:
        return f"{(left + 30) // 60} 分钟"
    return f"{left} 秒"


def format_confirm_dm(title: str, effect: str, code: str, short_id: str, ttl_seconds: int) -> str:
    shown = effect.strip() or "（调用方没有写批准后会发生什么）"
    window = _window_phrase(ttl_seconds)
    return (
        f"需要你确认一件事，{window}内有效。\n"
        "\n"
        f"事项：{title}\n"
        f"批准后会发生：{shown}\n"
        "\n"
        f"只回复这 6 位确认码：{code}\n"
        f"或打开网页控制台 → 审批中心 → 找到 #{short_id} → 批准 / 拒绝。\n"
        "\n"
        "超时未确认则此事取消，不会自动重做。"
    )


def _pick_ws_bot(ws_bot_id: str, bot_id: str, *, fallback_any: bool = True) -> Optional[_Bot]:
    from gsuid_core.gss import gss

    bot = gss.active_bot[ws_bot_id] if ws_bot_id and ws_bot_id in gss.active_bot else None
    if bot is None and bot_id in gss.active_bot:
        bot = gss.active_bot[bot_id]
    if bot is None and fallback_any and gss.active_bot:
        bot = next(iter(gss.active_bot.values()))
    return bot


async def _dm_user(bot_id: str, bot_self_id: str, user_id: str, ws_bot_id: str, text: str) -> bool:
    if not user_id:
        return False
    bot = _pick_ws_bot(ws_bot_id, bot_id, fallback_any=True)
    if bot is None:
        return False
    await bot.target_send(text, "direct", user_id, bot_id or bot.bot_id, bot_self_id or "")
    return True


async def _dm_master(row: AIApprovalRequest, text: str) -> bool:
    """主人级审批：直接私聊发给 masters 名单中的主人，不依赖订阅表。"""
    masters = core_config.get_config("masters")
    if not masters:
        return False
    ws_bot_id = _payload_str(row.payload_json, "ws_bot_id")
    bot = _pick_ws_bot(ws_bot_id, row.bot_id, fallback_any=True)
    if bot is None:
        return False
    bot_id = row.bot_id or bot.bot_id
    bot_self_id = row.bot_self_id or ""
    sent = 0
    for master_id in masters:
        uid = str(master_id).strip()
        if not uid:
            continue
        try:
            await bot.target_send(text, "direct", uid, bot_id, bot_self_id)
            sent += 1
        except Exception:
            logger.warning(t("log.ai.approval_confirm_dm_missing", p0=row.short_id))
    return sent > 0


async def send_confirm_dm(row: AIApprovalRequest, code: str) -> bool:
    """把确认码私聊给裁决人（用户级发给发起人，主人级发给主人）。"""
    left = max(1, int(row.expires_at) - int(time.time())) if row.expires_at else DEFAULT_APPROVAL_TTL
    text = format_confirm_dm(row.title, row.effect, code, row.short_id, left)
    if row.audience == "user":
        ok = await _dm_user(
            row.bot_id,
            row.bot_self_id,
            row.operator_user_id,
            _payload_str(row.payload_json, "ws_bot_id"),
            text,
        )
    else:
        ok = await _dm_master(row, text)
    if ok:
        logger.info(t("log.ai.approval_confirm_dm_sent", p0=row.short_id))
    else:
        logger.info(t("log.ai.approval_confirm_dm_missing", p0=row.short_id))
    return ok


async def notify_parent_group(row: AIApprovalRequest, text: str) -> None:
    """群里一句固定事实。若 text 为空则不发送，避免打扰群聊。"""
    if not text or not text.strip():
        return
    if row.user_type not in ("group", "channel", "sub_channel"):
        return
    target = row.group_id or ""
    if not target:
        return
    if row.user_type == "group":
        target_type = "group"
    elif row.user_type == "channel":
        target_type = "channel"
    else:
        target_type = "sub_channel"
    ws_bot_id = _payload_str(row.payload_json, "ws_bot_id")
    bot = _pick_ws_bot(ws_bot_id, row.bot_id, fallback_any=False)
    if bot is None:
        return
    await bot.target_send(
        text,
        target_type,
        target,
        row.bot_id,
        row.bot_self_id or "",
        group_id=row.group_id,
    )


async def notify_closed(row: AIApprovalRequest, *, expired: bool) -> None:
    """拒绝或超时：私聊告知裁决人，不发群。"""
    text = f"#{row.short_id} 已超时，未执行。" if expired else f"#{row.short_id} 已拒绝，未执行。"
    if row.audience == "user":
        await _dm_user(
            row.bot_id,
            row.bot_self_id,
            row.operator_user_id,
            _payload_str(row.payload_json, "ws_bot_id"),
            text,
        )
    else:
        await _dm_master(row, text)
    logger.info(t("log.ai.approval_expired_stop", p0=row.short_id, status="expired" if expired else "rejected"))


async def ack_direct(bot_id: str, bot_self_id: str, user_id: str, ws_bot_id: str, text: str) -> None:
    await _dm_user(bot_id, bot_self_id, user_id, ws_bot_id, text)


# ── 批准后续作回灌（把续作卡回灌进提交时的 agent）────────────────


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
        await notify_parent_group(row, (result_text or "已批准，原来的会话不在了。")[:800])
        return
    card = await _card(row, result_text)
    raw_bot = _pick_ws_bot(_payload_str(row.payload_json, "ws_bot_id"), row.bot_id, fallback_any=False)
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
        await AIAgentTask.update_data_by_data(select_data={"id": task.id}, update_data={"status": "running"})
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
    if task is not None:
        await kanban.mark_subtask_failed(task, reason[:1000])


async def _pause_task(task_id: str) -> None:
    from gsuid_core.ai_core.planning.models import AIAgentTask

    await AIAgentTask.update_data_by_data(select_data={"id": task_id}, update_data={"status": "waiting_approval"})


async def _settle_capability(row: AIApprovalRequest, raw: str, bot: Optional[Bot]) -> None:
    if row.category != "kanban_subtask" or not row.ref_key:
        return
    from gsuid_core.ai_core.planning import kanban
    from gsuid_core.ai_core.planning.models import AIAgentTask
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
