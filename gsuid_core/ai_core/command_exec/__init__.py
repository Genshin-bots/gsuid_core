"""GsCore AI 命令执行器（Command Executor）。

主人专属的系统命令通道：以审批与白名单为核心、引号感知的 stdlib 结构分析为安全底座、
受管沙盒为执行边界。分五道纵深闸门：身份 / 语法分析 / 策略 / 执行隔离 / 审计。
"""

from gsuid_core.i18n import t
from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import ai_config
from gsuid_core.ai_core.command_exec.models import (
    cfg_get,
    command_exec_config,
    cleanup_expired_audit,
)
from gsuid_core.ai_core.command_exec.policy import (
    Decision,
    CommandPlan,
    decide,
    analyze,
    register_command_approval_category,
)
from gsuid_core.ai_core.command_exec.executor import ExecResult, run_argv, run_resolved


def _schedule_audit_ttl_cleanup() -> None:
    """每日 04:30 清理低风险过期审计（幂等 replace_existing；高危/补全永久留存）。"""
    from gsuid_core.aps import scheduler

    async def _job() -> None:
        try:
            await cleanup_expired_audit()
        except Exception as e:
            logger.exception(t("log.ai.commandexec_audit_ttl_cleanup", e=e))

    scheduler.add_job(
        func=_job,
        trigger="cron",
        hour=4,
        minute=30,
        id="command_exec_audit_ttl_cleanup",
        name="命令执行审计 TTL 清理（每日 04:30）",
        replace_existing=True,
    )
    logger.info(t("log.ai.commandexec_audit_ttl_cleanup_register"))


async def init_command_exec() -> None:
    if not ai_config.get_config("enable").data:
        logger.info(t("log.ai.commandexec_master_switch_skipping"))
        return
    if not command_exec_config.get_config("enable").data:
        logger.info(t("log.ai.commandexec_enabled_skipping_webconsole"))
        return
    # 导入 tools 即触发 @ai_tools 注册；总开关或执行器关闭时不提前注册工具。
    import gsuid_core.ai_core.command_exec.tools  # noqa: F401

    register_command_approval_category()
    _schedule_audit_ttl_cleanup()
    logger.info(t("log.ai.commandexec_initialization"))


__all__ = [
    "analyze",
    "CommandPlan",
    "decide",
    "Decision",
    "run_argv",
    "ExecResult",
    "run_resolved",
    "cfg_get",
    "command_exec_config",
    "init_command_exec",
]
