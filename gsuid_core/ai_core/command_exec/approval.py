"""闸门 · 命令审批（统一审批中心 ``command_exec`` 领域适配层）。

审批账本 / 裁决入口 / 过期 / 待审批可见性全部收编进 ``ai_core.approval``；本模块
只剩领域逻辑：提交时冻结 argv 快照，批准回调里重跑 policy（防审批期间黑名单被改）
后执行快照并审计。防偷梁换柱（只执行快照）、防张冠李戴（多条待审批需指名）不变。
"""

import json
from typing import List, Optional

from gsuid_core.models import Event
from gsuid_core.ai_core import approval as approval_center
from gsuid_core.ai_core.approval import AIApprovalRequest
from gsuid_core.ai_core.command_exec import audit
from gsuid_core.ai_core.command_exec.config import cfg_get
from gsuid_core.ai_core.command_exec.policy import Decision, decide
from gsuid_core.ai_core.command_exec.runner import run_resolved
from gsuid_core.ai_core.command_exec.analyzer import CommandPlan, SimpleCommand, _classify

CATEGORY = "command_exec"


async def submit(ev: Optional[Event], plan: "CommandPlan", reason: str, request_id: str) -> AIApprovalRequest:
    """把 analyze 后的 argv 快照提交进审批中心（pending）。执行的永远是这份快照。"""
    argv = plan.commands[0].argv if plan.commands else []
    return await approval_center.submit(
        category=CATEGORY,
        title=f"`{plan.raw}`（{reason}）",
        ev=ev,
        audience="master",
        ref_key=request_id,
        payload={"raw": plan.raw, "argv": argv, "risk": plan.risk, "reason": reason},
    )


def _rebuild_plan(raw: str, argv: List[str]) -> CommandPlan:
    executable = argv[0] if argv else ""
    from pathlib import Path

    sc = SimpleCommand(argv=argv, executable=Path(executable).name.lower() if executable else "")
    plan = CommandPlan(raw=raw, ok=bool(argv), commands=[sc] if argv else [])
    if argv:
        plan.touches_network, plan.risk = _classify(sc)
    return plan


def _rebuild_event(req: AIApprovalRequest) -> Event:
    """从审批行还原执行用 Event（与 kanban_executor._build_event 同思路）。"""
    return Event(
        bot_id=req.bot_id,
        user_id=req.operator_user_id,
        bot_self_id=req.bot_self_id,
        user_type=req.user_type if req.user_type in ("group", "direct", "channel", "sub_channel") else "direct",
        group_id=req.group_id,
        real_bot_id=req.bot_id,
        msg_id="",
        user_pm=0,  # 命令审批 audience=master，裁决人必为主人
    )


def _format_result(command: str, result) -> str:
    body = result.stdout or "（无输出）"
    if result.truncated:
        body += "\n[输出已截断]"
    head = f"✅ 已执行 `{command}`" if result.returncode == 0 else f"⚠️ `{command}` 返回码 {result.returncode}"
    return f"{head}\n```\n{body}\n```"


async def _on_resolve(req: AIApprovalRequest, approved: bool, note: str) -> str:
    """审批中心裁决回调：批准 → 复核 policy → 执行 argv 快照 → 审计。"""
    payload = json.loads(req.payload_json or "{}")
    raw = str(payload["raw"]) if "raw" in payload else req.title
    argv = [str(x) for x in payload["argv"]] if "argv" in payload and isinstance(payload["argv"], list) else []

    if not approved:
        return f"🚫 已拒绝审批 #{req.short_id}：`{raw}`。"

    plan = _rebuild_plan(raw, argv)
    # 防审批期间黑名单被改：执行前再判一次，只拦「现在变成 DENY」的情况。
    decision, why = decide(plan)
    if decision is Decision.DENY:
        return f"🚫 复核未通过，取消执行 #{req.short_id}：{why}"

    ev = _rebuild_event(req)
    result, exec_err = await run_resolved(ev, argv, int(cfg_get("default_timeout")))
    if result is None:
        return f"❌ 执行失败：{exec_err}"
    await audit.log(ev, plan, Decision.APPROVAL, "主人审批通过并执行", request_id=req.ref_key, result=result)
    return _format_result(raw, result)


def register_command_approval_category() -> None:
    """注册 command_exec 审批领域（TTL 用命令执行器自己的 approval_ttl_seconds）。"""
    approval_center.register_approval_category(CATEGORY, _on_resolve, ttl_seconds=int(cfg_get("approval_ttl_seconds")))
