"""控制面工具：查在途委派 / 申辩框架指令。

``check_delegation`` 让「随时查子代理」成为一等能力——旧版模型只能猜 ordinal 或
拿到查不到的 8 字符前缀（见 control/delegation.py 的 INV-5 说明）。

``dispute_directive`` 是模型对框架观察的**结构化拒绝出口**。缺了它，模型认为
校验误报时只能在对用户可见的台词里反驳框架（生产 OOC「识图任务早就跑完了」
「识图连续几轮都有点抽风」正是如此）。申辩只进日志与 settle 结算，不对用户可见。
"""

from __future__ import annotations

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.control.directive import DISPUTE_EXTRA_KEY
from gsuid_core.ai_core.control.delegation import (
    await_delegation,
    format_delegation,
)

_MAX_WAIT_SEC = 20.0


@ai_tools(category="buildin", capability_domain="产物")
async def check_delegation(
    ctx: RunContext[ToolContext],
    delegation_id: str,
    wait_sec: float = 0.0,
) -> str:
    """查在途委派的真实状态与产物；``wait_sec>0`` 则等到终态或超时。

    ``delegation_id`` 用 create_subagent 回执里的 ``dlg_…`` 句柄（裸 root_task_id
    亦可）。用户追问进度时用 ``wait_sec=0`` 立即读；你打算等一下再答用 >0。
    句柄只进本工具参数，**禁止**写进给用户看的台词。
    """
    _ = ctx
    wait = max(0.0, min(float(wait_sec), _MAX_WAIT_SEC))
    deleg = await await_delegation(delegation_id, wait_sec=wait)
    if deleg is None:
        return f"⚠️ 委派不存在或已过期: {delegation_id}"
    return format_delegation(deleg)


@ai_tools(category="buildin", capability_domain="产物")
async def dispute_directive(ctx: RunContext[ToolContext], reason: str) -> str:
    """框架内部校验的观察与事实不符时用本工具申辩，而**不要**对用户解释。

    典型场景：校验说「你把长信息念成了台词」但你写的是用户点名要的叙事文/代码；
    或说「已有事实包未出图」但本轮根本没查数。申辩后你上一条回复照原样交付，
    不必改写、不必道歉、不必向用户提这件事。
    """
    body = (reason or "").strip()
    if not body:
        return "⚠️ 申辩需要给出理由（说明观察与事实哪里不符）。"
    extra = ctx.deps.extra
    bucket = extra[DISPUTE_EXTRA_KEY] if DISPUTE_EXTRA_KEY in extra else None
    if not isinstance(bucket, list):
        bucket = []
        extra[DISPUTE_EXTRA_KEY] = bucket
    bucket.append(body[:500])
    logger.info(t("log.agent.directive_disputed", reason=body[:120]))
    return "已记录申辩：本轮不必改写上一条回复，也不要向用户提及本次校验。"
