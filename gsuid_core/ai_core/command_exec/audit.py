"""闸门⑤：审计。每次决策+执行落库,可回溯（audit_enabled 控制）。

决策阶段先写一条（status=decided/denied/approval），执行后 finish 补 result——
避免「跑了却没记上」（§14.12）。输出只存双端截断摘要,防 DB 膨胀 / 敏感输出泄露。
"""

import json
from typing import TYPE_CHECKING, List, Optional

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.command_exec.config import cfg_get
from gsuid_core.ai_core.command_exec.models import AICommandAudit
from gsuid_core.ai_core.command_exec.executor import clip

if TYPE_CHECKING:
    from gsuid_core.ai_core.command_exec.policy import Decision
    from gsuid_core.ai_core.command_exec.analyzer import CommandPlan
    from gsuid_core.ai_core.command_exec.executor import ExecResult


def _argv_json(plan: "CommandPlan") -> str:
    argv: List[str] = plan.commands[0].argv if plan.commands else []
    return json.dumps(argv, ensure_ascii=False)


async def log(
    ev: Optional[Event],
    plan: "CommandPlan",
    decision: "Decision",
    reason: str,
    *,
    request_id: str = "",
    action: str = "exec",
    result: "Optional[ExecResult]" = None,
) -> Optional[int]:
    """写一条决策审计；若同时给了 result 则一并落执行结果。返回行 id。"""
    if not cfg_get("audit_enabled"):
        return None

    risk = plan.risk
    findings = list(plan.findings)
    status = _status_for(decision.value)
    returncode: Optional[int] = None
    excerpt = ""
    if result is not None:
        status = "executed"
        returncode = result.returncode
        excerpt = clip(result.stdout)
        if result.is_batch:
            findings.append("批处理脚本(.bat/.cmd)")
            risk = "high"

    row = await AICommandAudit.add(
        request_id=request_id,
        session_id=_session_id(ev),
        operator_user_id=str(ev.user_id) if ev else "",
        bot_id=ev.bot_id if ev else "",
        raw_command=plan.raw,
        argv_json=_argv_json(plan),
        decision=decision.value,
        reason=reason,
        risk=risk,
        touches_network=plan.touches_network,
        action=action,
        status=status,
        returncode=returncode,
        output_excerpt=excerpt,
        findings="; ".join(findings),
    )
    return row.id


async def finish(row_id: Optional[int], result: "ExecResult", status: str = "executed") -> None:
    """审批放行后单独执行时,把结果补回决策阶段那条审计。"""
    if row_id is None or not cfg_get("audit_enabled"):
        return
    await AICommandAudit.finish(
        row_id=row_id,
        status=status,
        returncode=result.returncode,
        output_excerpt=clip(result.stdout),
    )


def _status_for(decision: str) -> str:
    if decision == "deny":
        return "denied"
    if decision == "approval":
        return "pending"
    return "decided"


def _session_id(ev: Optional[Event]) -> str:
    return ev.session_id if ev else ""


async def cleanup_expired() -> int:
    """删早于 TTL 的低风险审计（高危 / provision 永久留存）。返回删除行数。"""
    n = await AICommandAudit.delete_expired(int(cfg_get("audit_ttl_days")))
    if n > 0:
        logger.info(t("🧰 [CommandExec] TTL 清理删除 {n} 条低风险审计", n=n))
    return n
