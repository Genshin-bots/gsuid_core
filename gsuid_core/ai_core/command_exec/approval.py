"""闸门 · 人在环审批（HITL）。

异步不阻塞：提交 pending → 返回「已提交审批」→ 主人下一条消息表态 → AI 调
respond_command_approval 转达 → 执行入库 argv 快照。防偷梁换柱（只执行快照,执行前
再 policy.decide 一次）、防张冠李戴（多条待审批需指名）。见设计文档 §6。
"""

import json
import secrets
from typing import Set, List, Optional

from gsuid_core.models import Event
from gsuid_core.ai_core.command_exec import audit
from gsuid_core.ai_core.command_exec.config import cfg_get
from gsuid_core.ai_core.command_exec.models import AICommandApproval
from gsuid_core.ai_core.command_exec.policy import Decision, decide
from gsuid_core.ai_core.command_exec.runner import run_resolved
from gsuid_core.ai_core.command_exec.analyzer import CommandPlan, SimpleCommand, _classify

# 内存待审批标记：给 visible_when 谓词做「廉价内存判定」用(不每步查库,对齐 SKILL §7.6)。
# 允许略微过可见(err on visible),不影响 check_func 与 resolve 的严格校验。
_PENDING_OPERATORS: Set[str] = set()


def has_pending(user_id: str) -> bool:
    return str(user_id) in _PENDING_OPERATORS


async def prime_pending() -> None:
    """重启后回填内存待审批标记：否则 DB 有 pending 但 respond/list 工具全被隐藏成死锁。"""
    for uid in await AICommandApproval.pending_operator_ids():
        _PENDING_OPERATORS.add(str(uid))


async def submit(ev: Optional[Event], plan: CommandPlan, reason: str, request_id: str) -> AICommandApproval:
    """把 analyze 后的 argv 快照入库 pending。执行的永远是这份快照。"""
    argv = plan.commands[0].argv if plan.commands else []
    row = await AICommandApproval.add(
        request_id=request_id,
        short_id=secrets.token_hex(2),
        session_id=ev.session_id if ev else "",
        operator_user_id=str(ev.user_id) if ev else "",
        bot_id=ev.bot_id if ev else "",
        bot_self_id=ev.bot_self_id if ev else "",
        user_type=ev.user_type if ev else "direct",
        group_id=ev.group_id if ev else None,
        raw_command=plan.raw,
        argv_json=json.dumps(argv, ensure_ascii=False),
        reason=reason,
        risk=plan.risk,
        action="exec",
    )
    if ev is not None:
        _PENDING_OPERATORS.add(str(ev.user_id))
    return row


def _sync_pending_flag(owner: str, rows: List[AICommandApproval]) -> None:
    if rows:
        _PENDING_OPERATORS.add(owner)
    else:
        _PENDING_OPERATORS.discard(owner)


async def _refresh_pending(owner: str) -> None:
    rows = await AICommandApproval.list_pending_by_operator(owner)
    _sync_pending_flag(owner, rows)


async def list_pending(ev: Optional[Event]) -> str:
    if ev is None:
        return "⚠️ 无会话信息。"
    await AICommandApproval.expire_stale(int(cfg_get("approval_ttl_seconds")))
    rows = await AICommandApproval.list_pending_by_operator(str(ev.user_id))
    _sync_pending_flag(str(ev.user_id), rows)
    if not rows:
        return "ℹ️ 当前没有待审批命令。"
    lines = [f"#{r.short_id} `{r.raw_command}`（{r.reason}）" for r in rows]
    return "⏳ 待审批命令：\n" + "\n".join(lines)


async def resolve(
    ev: Optional[Event],
    request_ref: str,
    approved: bool,
    note: str = "",
    via: str = "chat",
) -> str:
    if ev is None:
        return "⚠️ 无会话信息。"
    await AICommandApproval.expire_stale(int(cfg_get("approval_ttl_seconds")))

    owner = str(ev.user_id)
    target, err = await _locate(owner, request_ref)
    if err:
        return err
    if target is None or target.id is None:
        return "ℹ️ 未找到对应的待审批命令,可能已失效,请重新发起。"

    if _is_expired(target):
        await AICommandApproval.mark(target.id, "expired", note, via)
        return "⌛ 该审批已失效（超过有效期）,请重新发起。"

    if not approved:
        await AICommandApproval.mark(target.id, "denied", note, via)
        await _refresh_pending(owner)
        return f"🚫 已拒绝审批 #{target.short_id}：`{target.raw_command}`。"

    argv: List[str] = json.loads(target.argv_json or "[]")
    plan = _rebuild_plan(target.raw_command, argv)
    # 防审批期间黑名单被改:执行前再判一次,只拦「现在变成 DENY」的情况。
    decision, why = decide(plan)
    if decision is Decision.DENY:
        await AICommandApproval.mark(target.id, "denied", f"复核拒绝: {why}", via)
        return f"🚫 复核未通过,取消执行 #{target.short_id}：{why}"

    result, exec_err = await run_resolved(ev, argv, int(cfg_get("default_timeout")))
    if result is None:
        await AICommandApproval.mark(target.id, "denied", f"执行失败: {exec_err}", via)
        return f"❌ 执行失败：{exec_err}"

    await AICommandApproval.mark(target.id, "executed", note, via)
    await _refresh_pending(owner)
    await audit.log(ev, plan, Decision.APPROVAL, "主人审批通过并执行", request_id=target.request_id, result=result)
    return _format_result(target.raw_command, result)


async def _locate(owner: str, request_ref: str) -> tuple[Optional[AICommandApproval], str]:
    """按 ref 精确定位；否则名下唯一 pending 直接命中,多条则要求指名。"""
    ref = request_ref.strip().lstrip("#")
    if ref:
        row = await AICommandApproval.get_by_short_id(ref)
        if row is None or row.status != "pending" or row.operator_user_id != owner:
            return None, f"ℹ️ 未找到编号 #{ref} 的待审批命令（可能已处理或失效）。"
        return row, ""

    rows = await AICommandApproval.list_pending_by_operator(owner)
    if not rows:
        return None, "ℹ️ 当前没有等待你审批的命令。"
    if len(rows) > 1:
        listing = "、".join(f"#{r.short_id} {r.raw_command}" for r in rows[:5])
        return None, f"❓ 存在多个待审批命令（{listing}）,请回复「同意 #{rows[0].short_id}」指明是哪条。"
    return rows[0], ""


def _rebuild_plan(raw: str, argv: List[str]) -> CommandPlan:
    executable = argv[0] if argv else ""
    from pathlib import Path

    sc = SimpleCommand(argv=argv, executable=Path(executable).name.lower() if executable else "")
    plan = CommandPlan(raw=raw, ok=bool(argv), commands=[sc] if argv else [])
    if argv:
        plan.touches_network, plan.risk = _classify(sc)
    return plan


def _is_expired(row: AICommandApproval) -> bool:
    import time

    ttl = int(cfg_get("approval_ttl_seconds"))
    return ttl > 0 and int(time.time()) - row.created_at > ttl


def _format_result(command: str, result) -> str:
    body = result.stdout or "（无输出）"
    if result.truncated:
        body += "\n[输出已截断]"
    head = f"✅ 已执行 `{command}`" if result.returncode == 0 else f"⚠️ `{command}` 返回码 {result.returncode}"
    return f"{head}\n```\n{body}\n```"
