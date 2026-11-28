"""命令执行器：配置定义、审计数据表（AICommandAudit）与审计读写。"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, List, Optional

from sqlmodel import Field, col, delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.data_store import get_res_path
from gsuid_core.utils.database.base_models import BaseIDModel, with_session
from gsuid_core.utils.plugins_config.models import (
    GSC,
    GSCData,
    GsDivider,
    GsIntConfig,
    GsStrConfig,
    GsBoolConfig,
    GsDictConfig,
    GsListStrConfig,
)
from gsuid_core.utils.plugins_config.gs_config import StringConfig

if TYPE_CHECKING:
    from gsuid_core.ai_core.command_exec.policy import Decision, CommandPlan
    from gsuid_core.ai_core.command_exec.executor import ExecResult

# ── 独立配置（StringConfig · 热重载）────────────────────────────

COMMAND_EXEC_CONFIG: dict[str, GSC] = {
    "enable": GsBoolConfig(
        "启用命令执行器",
        "总开关；关闭后 run_command 直接拒绝",
        False,
    ),
    "_AuthDivider": GsDivider("身份与授权", "谁能触发", "身份与授权"),
    "operator_user_ids": GsListStrConfig(
        "额外授权用户ID",
        "主人(masters)之外、额外允许触发命令执行器的 user_id 列表；留空=仅主人",
        [],
    ),
    "_ApprovalDivider": GsDivider("审批策略", "命令放行档位", "审批策略"),
    "approval_mode": GsStrConfig(
        "审批模式",
        "all=全部命令都需主人审批; smart=白名单自动/黑名单拒绝/其余审批; "
        "auto=仅黑名单拒绝其余自动(高风险,仅信任的单人服务器)",
        "smart",
        options=["all", "smart", "auto"],
    ),
    "auto_allow_commands": GsListStrConfig(
        "自动放行命令白名单",
        "smart/auto 模式下免审批直接执行的可执行文件——只应放只读/信息类命令。"
        "解释器/包管理器/联网工具(python/node/npm/git/curl...)即便写进这里也不会走快速通道:"
        "它们在 NEVER_AUTO_ALLOW 元工具名单里(代码级),会被强制转单次审批,防越权后门。",
        [
            "ls",
            "cat",
            "echo",
            "pwd",
            "whoami",
            "which",
            "where",
            "head",
            "tail",
            "wc",
            "date",
            "uname",
            "hostname",
            "df",
            "free",
        ],
    ),
    "deny_commands": GsListStrConfig(
        "永久拒绝命令黑名单",
        "这些可执行文件在任何模式下都拒绝(即便主人审批也不放行)",
        [
            "rm",
            "rmdir",
            "del",
            "mkfs",
            "dd",
            "fdisk",
            "parted",
            "format",
            "shutdown",
            "reboot",
            "poweroff",
            "sudo",
            "su",
            "chmod",
            "chown",
            "kill",
            "killall",
            "taskkill",
            "nc",
            "ncat",
            "netcat",
            "iptables",
            "reg",
            "diskpart",
        ],
    ),
    "path_arg_policy": GsStrConfig(
        "参数路径越界策略",
        "自动放行的命令若参数含逃出沙盒的路径(绝对路径/../): approval=转单次审批(默认); "
        "deny=直接拒; off=不检查。仅在'本要免审批'时触发,不影响审批/黑名单档。",
        "approval",
        options=["approval", "deny", "off"],
    ),
    "_NetworkDivider": GsDivider("网络与下载", "外联控制", "网络与下载"),
    "allow_network": GsBoolConfig(
        "允许联网命令",
        "curl/wget/git clone/npm install/pip install 等外联操作",
        True,
    ),
    "require_approval_for_network": GsBoolConfig(
        "联网命令强制审批",
        "即便 allow_network 开启,联网命令仍逐次审批",
        True,
    ),
    "allow_auto_provision": GsBoolConfig(
        "允许自动安装缺失工具",
        "缺 npm/node 等时尝试装到受管工具链目录(高风险,默认关)",
        False,
    ),
    "provision_mirror_urls": GsDictConfig(
        "自动安装镜像源",
        "工具→[下载基址] 映射(值为单元素列表,受 GsDictConfig 类型约束);把配方里的官方源换成"
        "国内镜像(如 node→['https://npmmirror.com/mirrors/node'])。sha256 仍取自官方配方,"
        "对得上就安全。留空=用配方内置官方源。只填已知镜像白名单基址,不接受任意 URL。",
        {},
    ),
    "_LimitDivider": GsDivider("执行限制", "资源与沙盒", "执行限制"),
    "default_timeout": GsIntConfig("默认超时(秒)", "单条命令默认超时", 60),
    "max_timeout": GsIntConfig("最大超时(秒)", "允许的最大超时上限", 600),
    "max_output_bytes": GsIntConfig("最大输出字节", "stdout+stderr 合并输出上限", 1024 * 1024),
    "sandbox_dir": GsStrConfig("沙盒工作目录", "命令默认 cwd;留空=data/ai_core/file", ""),
    "approval_ttl_seconds": GsIntConfig("审批有效期(秒)", "pending 请求超时自动作废", 1800),
    "_AuditDivider": GsDivider("审计", "留痕", "审计"),
    "audit_enabled": GsBoolConfig("启用审计", "每次决策+执行落库", True),
    "audit_ttl_days": GsIntConfig(
        "审计保留天数",
        "每日清理早于该天数的低风险审计;0=永不清理;高危/补全永久留存",
        30,
    ),
    "notify_master_on_exec": GsBoolConfig("执行后通知主人", "自动放行的命令执行后也私信主人一条摘要", False),
}

command_exec_config = StringConfig(
    "GsCore AI 命令执行器配置",
    get_res_path("ai_core") / "command_exec_config.json",
    COMMAND_EXEC_CONFIG,
)


def cfg_get(key: str) -> GSCData:
    """读某配置项的 data；每次现读以支持热重载。"""
    return command_exec_config.get_config(key).data


def cfg_bool(key: str) -> bool:
    return bool(cfg_get(key))


def cfg_int(key: str, default: int = 0) -> int:
    val = cfg_get(key)
    if isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    return default


def cfg_str(key: str) -> str:
    val = cfg_get(key)
    return val if isinstance(val, str) else ""


def cfg_str_list(key: str) -> list[str]:
    val = cfg_get(key)
    if not isinstance(val, list):
        return []
    return [str(x) for x in val]


def cfg_dict_str_list(key: str) -> dict[str, list[str]]:
    val = cfg_get(key)
    if not isinstance(val, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in val.items():
        if isinstance(k, str) and isinstance(v, list):
            out[k] = [str(item) for item in v]
    return out


# ── 审计数据表（AICommandAudit）与审计读写 ───────────────────────


class AICommandAudit(BaseIDModel, table=True):
    """命令执行审计表：每次决策+执行留痕（先写决策、执行后补结果）。"""

    __table_args__ = {"extend_existing": True}

    request_id: str = Field(default="", index=True, max_length=36)
    session_id: str = Field(default="", index=True, max_length=128)
    operator_user_id: str = Field(default="", index=True, max_length=64)
    bot_id: str = Field(default="", max_length=64)
    raw_command: str = Field(default="")
    argv_json: str = Field(default="[]")
    decision: str = Field(default="", max_length=16)
    reason: str = Field(default="")
    risk: str = Field(default="low", max_length=8)
    touches_network: bool = Field(default=False)
    action: str = Field(default="exec", max_length=16)
    status: str = Field(default="decided", max_length=16)
    returncode: Optional[int] = Field(default=None)
    output_excerpt: str = Field(default="")
    findings: str = Field(default="")
    created_at: int = Field(default=0, index=True)
    finished_at: int = Field(default=0)

    @classmethod
    @with_session
    async def add(
        cls,
        session: AsyncSession,
        *,
        request_id: str = "",
        session_id: str = "",
        operator_user_id: str = "",
        bot_id: str = "",
        raw_command: str = "",
        argv_json: str = "[]",
        decision: str = "",
        reason: str = "",
        risk: str = "low",
        touches_network: bool = False,
        action: str = "exec",
        status: str = "decided",
        returncode: Optional[int] = None,
        output_excerpt: str = "",
        findings: str = "",
        created_at: Optional[int] = None,
    ) -> "AICommandAudit":
        row = cls(
            request_id=request_id,
            session_id=session_id,
            operator_user_id=operator_user_id,
            bot_id=bot_id,
            raw_command=raw_command,
            argv_json=argv_json,
            decision=decision,
            reason=reason,
            risk=risk,
            touches_network=touches_network,
            action=action,
            status=status,
            returncode=returncode,
            output_excerpt=output_excerpt,
            findings=findings,
            created_at=created_at if created_at is not None else int(time.time()),
        )
        session.add(row)
        await session.flush()
        return row

    @classmethod
    @with_session
    async def finish(
        cls,
        session: AsyncSession,
        row_id: int,
        status: str,
        returncode: Optional[int],
        output_excerpt: str,
    ) -> None:
        row = await session.get(cls, row_id)
        if row is None:
            return
        row.status = status
        row.returncode = returncode
        row.output_excerpt = output_excerpt
        row.finished_at = int(time.time())
        session.add(row)

    @classmethod
    @with_session
    async def delete_expired(cls, session: AsyncSession, ttl_days: int) -> int:
        """删早于 TTL 的低风险审计；永久保留 high risk 与 provision 动作。"""
        if ttl_days <= 0:
            return 0
        before = int(time.time()) - ttl_days * 86400
        stmt = delete(cls).where(
            col(cls.created_at) < before,
            col(cls.risk) != "high",
            col(cls.action) != "provision",
        )
        result = await session.execute(stmt)
        return result.rowcount if isinstance(result, CursorResult) else 0


def _argv_json(plan: "CommandPlan") -> str:
    argv: List[str] = plan.commands[0].argv if plan.commands else []
    return json.dumps(argv, ensure_ascii=False)


def _status_for(decision: str) -> str:
    if decision == "deny":
        return "denied"
    if decision == "approval":
        return "pending"
    return "decided"


async def audit_log(
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
    if not cfg_bool("audit_enabled"):
        return None
    from gsuid_core.ai_core.command_exec.executor import clip

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
        session_id=ev.session_id if ev else "",
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


async def cleanup_expired_audit() -> int:
    """删早于 TTL 的低风险审计（高危 / provision 永久留存）。返回删除行数。"""
    n = await AICommandAudit.delete_expired(cfg_int("audit_ttl_days"))
    if n > 0:
        logger.info(t("log.ai.commandexec_ttl_cleanup_removed_delete", n=n))
    return n
