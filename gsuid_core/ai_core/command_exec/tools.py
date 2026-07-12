"""命令执行器 @ai_tools 注册入口（category="buildin",主人专属）。

run_command（主入口）/ check_command_available。审批的转达 / 列表已统一到
``buildin_tools/approval_tools.py`` 的 ``respond_approval`` / ``list_pending_approvals``
（全框架一个转达工具）。靠 visible_when 对非主人隐藏 schema、check_func 执行期兜底。
"""

import uuid
import shutil
from typing import Tuple, Optional

from pydantic_ai import RunContext

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.ai_core.command_exec import audit, approval
from gsuid_core.ai_core.command_exec.config import cfg_get
from gsuid_core.ai_core.command_exec.policy import Decision, decide
from gsuid_core.ai_core.command_exec.runner import run_resolved
from gsuid_core.ai_core.command_exec.analyzer import CommandPlan, analyze


def _is_authorized(ev: Optional[Event]) -> bool:
    if ev is None:
        return False
    from gsuid_core.ai_core.utils import _is_master_user

    if _is_master_user(str(ev.user_id)) or ev.user_pm == 0:
        return True
    return str(ev.user_id) in set(cfg_get("operator_user_ids") or [])


def _cmd_visible_to_master(ctx: "RunContext[ToolContext]") -> bool:
    if not cfg_get("enable"):
        return False
    ev = ctx.deps.ev if ctx.deps else None
    if ev is None:
        return True  # 不隐藏,交 check_func 兜底(不误伤能力代理)
    return _is_authorized(ev)


def _master_and_enabled(ev: Optional[Event]) -> Tuple[bool, str]:
    if not cfg_get("enable"):
        return False, "🚫 命令执行器未启用(可在 WebConsole 开启)"
    if not _is_authorized(ev):
        return False, "🚫 仅主人可使用命令执行器"
    return True, "✅"


def _format_result(command: str, result) -> str:
    body = result.stdout or "（无输出）"
    if result.truncated:
        body += "\n[输出已截断]"
    head = f"✅ 已执行 `{command}`" if result.returncode == 0 else f"⚠️ `{command}` 返回码 {result.returncode}"
    return f"{head}\n```\n{body}\n```"


async def _execute_and_report(ev: Optional[Event], plan: CommandPlan, timeout: int, work_dir: Optional[str]) -> str:
    argv = plan.commands[0].argv
    result, err = await run_resolved(ev, argv, timeout, work_dir)
    if result is None:
        await audit.log(ev, plan, Decision.ALLOW, f"执行失败: {err}")
        return f"❌ 执行失败：{err}"
    await audit.log(ev, plan, Decision.ALLOW, "", result=result)
    if cfg_get("notify_master_on_exec"):
        logger.info(t("🧰 [CommandExec] 自动放行执行完成: {p0}", p0=plan.raw))
    return _format_result(plan.raw, result)


@ai_tools(category="buildin", check_func=_master_and_enabled, visible_when=_cmd_visible_to_master)
async def run_command(
    ctx: RunContext[ToolContext],
    command: str,
    timeout: int = 0,
    work_dir: Optional[str] = None,
) -> str:
    """在用户本地终端执行shell命令

    在用户本地终端执行shell命令 / 在服务器上执行一条系统命令(仅主人可用,受审批与白名单约束)。

    可执行 npm / curl / git / python 等 CLI。**只支持单条简单命令**(不支持管道 |、
    重定向 >、命令链 && ;、后台 &、命令替换 $());如需分步请多次调用。
    联网 / 安装类命令通常需要主人审批;主人在对话里回复'同意'后,再调用
    respond_approval 转达即可执行。**无交互环境**:命令必须带非交互标志
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
        await audit.log(ev, plan, decision, why)
        return f"🚫 拒绝执行：{why}"

    if decision is Decision.APPROVAL:
        request_id = uuid.uuid4().hex
        req = await approval.submit(ev, plan, why, request_id)
        await audit.log(ev, plan, decision, why, request_id=request_id)
        return (
            f"⏳ 已提交审批 #{req.short_id}：`{command}`（{why}）。"
            f"请主人回复'同意'或'拒绝'后,我再调用 respond_approval 转达。"
        )

    return await _execute_and_report(ev, plan, timeout, work_dir)


@ai_tools(category="buildin", check_func=_master_and_enabled, visible_when=_cmd_visible_to_master)
async def check_command_available(ctx: RunContext[ToolContext], name: str) -> str:
    """查某个可执行文件是否已安装及其绝对路径（只读,不执行任意命令）。

    Args:
        name: 可执行文件名,如 "node" / "npm" / "git"。
    """
    from pathlib import Path

    resolved = shutil.which(Path(name).name)
    if resolved is None:
        return f"❌ 未找到 '{name}'（不在 PATH 中）。"
    is_batch = Path(resolved).suffix.lower() in {".bat", ".cmd"}
    tag = "（⚠️ 批处理脚本 .bat/.cmd）" if is_batch else ""
    return f"✅ '{name}' 可用：{resolved}{tag}"
