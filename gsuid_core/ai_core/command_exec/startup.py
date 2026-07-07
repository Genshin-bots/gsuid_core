"""命令执行器启动接入：载配置 + 注册工具 + 预热 + 审计 TTL 清理 job。

由 ai_core/startup.py 的 _INIT_STEPS 统一调用。表由 create_all 统一建（models 已登记进
AI_DATABASE_MODEL_MODULES）。总开关关闭时直接返回、不注册重逻辑（SKILL §02 红线）。
"""

from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import ai_config


async def init_command_exec() -> None:
    if not ai_config.get_config("enable").data:
        logger.info("🧰 [CommandExec] AI总开关已关闭,跳过命令执行器初始化")
        return

    from gsuid_core.ai_core.command_exec.config import command_exec_config

    if not command_exec_config.get_config("enable").data:
        logger.info("🧰 [CommandExec] 未启用,跳过（可在 WebConsole 开启）")
        return

    # 导入即触发 @ai_tools 注册（run_command 等）。
    import gsuid_core.ai_core.command_exec.tools  # noqa: F401

    # 向统一审批中心注册 command_exec 领域（账本/裁决/过期均在审批中心）
    from gsuid_core.ai_core.command_exec.approval import register_command_approval_category

    register_command_approval_category()
    _schedule_audit_ttl_cleanup()
    logger.info("🧰 [CommandExec] 初始化完成")


def _schedule_audit_ttl_cleanup() -> None:
    """每日 04:30 清理低风险过期审计（幂等 replace_existing;高危/补全永久留存）。"""
    from gsuid_core.aps import scheduler
    from gsuid_core.ai_core.command_exec import audit

    async def _job() -> None:
        try:
            await audit.cleanup_expired()
        except Exception as e:
            logger.exception(f"🧰 [CommandExec] 审计 TTL 清理失败: {e}")

    scheduler.add_job(
        func=_job,
        trigger="cron",
        hour=4,
        minute=30,
        id="command_exec_audit_ttl_cleanup",
        name="命令执行审计 TTL 清理（每日 04:30）",
        replace_existing=True,
    )
    logger.info("🧰 [CommandExec] 审计 TTL 清理 job 已注册（每日 04:30）")
