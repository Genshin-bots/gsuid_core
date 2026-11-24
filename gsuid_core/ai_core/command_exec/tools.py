"""命令执行器 @ai_tools 注册与启动接入（category="buildin", 主人专属）。"""

from __future__ import annotations

import uuid
import shutil
from typing import Tuple, Optional
from pathlib import Path

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.command_exec.models import (
    cfg_bool,
    audit_log,
    cfg_str_list,
)
from gsuid_core.ai_core.command_exec.policy import (
    Decision,
    CommandPlan,
    decide,
    analyze,
    submit_approval,
    format_command_result,
)
from gsuid_core.ai_core.command_exec.executor import run_resolved


def _is_authorized(ev: Optional[Event]) -> bool:
    if ev is None:
        return False
    from gsuid_core.ai_core.utils import _is_master_user

    if _is_master_user(str(ev.user_id)) or ev.user_pm == 0:
        return True
    return str(ev.user_id) in set(cfg_str_list("operator_user_ids"))


def _cmd_visible_to_master(ctx: "RunContext[ToolContext]") -> bool:
    if not cfg_bool("enable"):
        return False
    ev = ctx.deps.ev if ctx.deps else None
    if ev is None:
        return True
    return _is_authorized(ev)


def _master_and_enabled(ev: Optional[Event]) -> Tuple[bool, str]:
    if not cfg_bool("enable"):
        return False, "🚫 命令执行器未启用(可在 WebConsole 开启)"
    if not _is_authorized(ev):
        return False, "🚫 仅主人可使用命令执行器"
    return True, "✅"


async def _execute_and_report(ev: Optional[Event], plan: CommandPlan, timeout: int, work_dir: Optional[str]) -> str:
    argv = plan.commands[0].argv
    result, err = await run_resolved(ev, argv, timeout, work_dir)
    if result is None:
        await audit_log(ev, plan, Decision.ALLOW, f"执行失败: {err}")
        return f"❌ 执行失败：{err}"
    await audit_log(ev, plan, Decision.ALLOW, "", result=result)
    if cfg_bool("notify_master_on_exec"):
        logger.info(t("log.ai.commandexec_auto_allow", p0=plan.raw))
    return format_command_result(plan.raw, result)


# 配置 max_timeout 默认上限 600s，外层包装需 ≥ 该上限，避免被默认 60s 误杀。
@ai_tools(
    category="buildin",
    check_func=_master_and_enabled,
    visible_when=_cmd_visible_to_master,
    timeout=600.0,
    code_callable=False,
)
async def run_command(
    ctx: RunContext[ToolContext],
    command: str,
    timeout: int = 0,
    work_dir: Optional[str] = None,
) -> str:
    """在用户本地终端执行并返回 shell 命令结果

    在用户本地终端执行shell命令 / 在服务器上执行一条系统命令(仅主人可用,受审批与白名单约束)。

    可执行 npm / curl / git / python 等 CLI。**只支持单条简单命令**(不支持管道 |、
    重定向 >、命令链 && ;、后台 &、命令替换 $());如需分步请多次调用。
    联网 / 安装类命令通常需要主人审批：提交后本轮结束，主人用私聊确认码或审批中心
    批准后才会执行。**无交互环境**:命令必须带非交互标志
    (npm install -y / pip --no-input / curl -sS 等),否则会等不到输入而失败或超时。

    Args:
        command: 要执行的命令,如 "npm install -y left-pad" 或 "curl -sSL https://example.com"。
        timeout: 超时秒数,0 表示用配置默认(default_timeout)。
        work_dir: 工作目录(必须位于框架沙盒之下),留空=沙盒根。
    """
    ev = ctx.deps.ev
    plan = analyze(command)
    decision, why = decide(plan)

    if decision is Decision.DENY:
        await audit_log(ev, plan, decision, why)
        return f"🚫 拒绝执行：{why}"

    if decision is Decision.APPROVAL:
        request_id = uuid.uuid4().hex
        req = await submit_approval(ev, plan, why, request_id)
        await audit_log(ev, plan, decision, why, request_id=request_id)
        from gsuid_core.ai_core.approval import submit_meta
        from gsuid_core.ai_core.approval.models import halt_line

        delivered, reused = submit_meta(req.request_id)
        return halt_line(req.short_id, delivered=delivered, reused=reused)

    return await _execute_and_report(ev, plan, timeout, work_dir)


@ai_tools(category="buildin", check_func=_master_and_enabled, visible_when=_cmd_visible_to_master)
async def check_command_available(ctx: RunContext[ToolContext], name: str) -> str:
    """查某个可执行文件是否已安装及其绝对路径（只读,不执行任意命令）。

    Args:
        name: 可执行文件名,如 "node" / "npm" / "git"。
    """
    resolved = shutil.which(Path(name).name)
    if resolved is None:
        return f"❌ 未找到 '{name}'（不在 PATH 中）。"
    is_batch = Path(resolved).suffix.lower() in {".bat", ".cmd"}
    tag = "（⚠️ 批处理脚本 .bat/.cmd）" if is_batch else ""
    return f"✅ '{name}' 可用：{resolved}{tag}"
