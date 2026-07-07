"""Kanban 任务编排层启动模块

由 ai_core 统一初始化入口在后台调用：

1. 注册 Kanban LLM 工具（``kanban_tools``）；
2. 注册内置能力代理画像（``capability_agents.profiles``）+ 用户自建画像；
3. 注册框架内部 ``capability_evaluator`` 代理（评估能力覆盖前置）；
4. 启动期僵尸子任务恢复：把因进程崩溃滞留在 ``running`` 的子任务复活，
   再对所有 ``running`` / ``pending`` 根任务统一 ``kick_root`` 一次。

数据表由 utils/database/startup.py 的 ``create_all`` 统一创建（planning.models
已登记进 AI_DATABASE_MODEL_MODULES）。
"""

from sqlmodel import col, select

from gsuid_core.logger import logger
from gsuid_core.ai_core.configs.ai_config import ai_config


async def init_planning() -> None:
    """初始化 Kanban 任务编排层。"""
    if not ai_config.get_config("enable").data:
        logger.info("📋 [Kanban] AI总开关已关闭，跳过任务编排层初始化")
        return

    # 导入即注册 Kanban LLM 工具
    import gsuid_core.ai_core.planning.kanban_tools  # noqa: F401

    # 向统一审批中心注册 kanban_subtask 领域（插件安装审批同属此领域）
    _register_kanban_approval_category()

    # 注册框架内置能力代理画像（6 个通用画像）
    try:
        from gsuid_core.ai_core.capability_agents.profiles import register_builtin_profiles

        register_builtin_profiles()
    except Exception as e:
        logger.exception(f"📋 [Kanban] 能力代理画像注册失败: {e}")

    # 内部能力评估代理（仅 evaluate_agent_mesh_capability 工具内部使用）
    try:
        from gsuid_core.ai_core.capability_agents.evaluator import register_capability_evaluator

        register_capability_evaluator()
    except Exception as e:
        logger.exception(f"📋 [Kanban] 能力评估代理注册失败: {e}")

    # webconsole 后端依赖：把磁盘上的用户自建画像挂回内存注册表。
    # 必须在 register_builtin_profiles 之后——同名时让用户版本覆盖内置版本，
    # 让前端"复制内置画像再改一改"的工作流可行。
    try:
        from gsuid_core.ai_core.capability_agents.persistence import load_user_profiles

        load_user_profiles()
    except Exception as e:
        logger.exception(f"📋 [Kanban] 用户自建能力代理画像加载失败: {e}")

    # 启动期僵尸子任务恢复
    try:
        await _recover_zombies_and_kick()
    except Exception as e:
        logger.exception(f"📋 [Kanban] 子任务崩溃恢复失败: {e}")

    # 启动期周期模板恢复：把 armed 模板重新挂到 APScheduler
    try:
        from .recurring import restore_armed_templates

        await restore_armed_templates()
    except Exception as e:
        logger.exception(f"📋 [Kanban] 周期模板恢复失败: {e}")

    # 启动期 not_before 子任务唤醒恢复：进程重启后 APScheduler 内存表丢失，
    # 重新把数据库里所有 pending 且未到期的子任务 not_before 挂回去。
    try:
        from .recurring import restore_pending_not_before_wakeups

        await restore_pending_not_before_wakeups()
    except Exception as e:
        logger.exception(f"📋 [Kanban] not_before 唤醒恢复失败: {e}")

    # 启动期周期子任务模板恢复：所有 armed 周期子任务重新挂回 APScheduler，
    # 让"管虚拟盘一个月""每日打卡 30 天"等长生命周期任务跨进程重启依然推进。
    try:
        from .recurring import restore_armed_subtask_templates

        await restore_armed_subtask_templates()
    except Exception as e:
        logger.exception(f"📋 [Kanban] 周期子任务模板恢复失败: {e}")

    # Artifact TTL 清理：每天 4:00 跑一次，删除 expires_at < now 的过期 artifact。
    # TTL 默认 30 天，由 workspace.put_artifact 在登记时写入；过期清理含落盘
    # 文件删除，详见 AIAgentArtifact.delete_expired。
    try:
        _schedule_artifact_ttl_cleanup()
    except Exception as e:
        logger.exception(f"📋 [Kanban] Artifact TTL 清理 job 注册失败: {e}")

    logger.info("📋 [Kanban] 任务编排层初始化完成")


def _register_kanban_approval_category() -> None:
    """kanban_subtask 审批领域：批准 → 子任务回 pending + kick_root；拒绝 → failed。

    TTL 设 7 天：插件安装等审批可能隔夜才被主人处理，不能按命令级 30 分钟过期。
    """
    import asyncio

    from gsuid_core.ai_core.approval import AIApprovalRequest, register_approval_category

    from . import kanban
    from .models import AIAgentTask

    async def _on_resolve(req: AIApprovalRequest, approved: bool, note: str) -> str:
        task = await AIAgentTask.get_by_id(req.ref_key)
        if task is None:
            return f"⚠️ 请求 #{req.short_id} 对应的子任务已不存在（可能已被清理）。"
        ok, msg = await kanban.approve_subtask(task, approved, note)
        if not ok:
            return f"⚠️ {msg}"
        if approved:
            from .kanban_executor import kick_root

            asyncio.create_task(kick_root(task.root_task_id or task.id))
            # 批准 ≠ 完成：拦住人格在这一步就宣布"已搞定"（实测会话 fa7eef 踩坑）
            return (
                "✅ 已转达批准，专职助手已被重新调度、正在**继续执行剩余步骤**"
                "（如插件的实际安装 → 热加载 → 功能自测，现在都还没完成）。\n"
                "⚠️ 现在请只简短回主人「好的，正在装 / 正在继续」之类的话，**切勿**说"
                "「已安装好 / 已搞定 / 现在能用了 / 测试通过」——这些都还没发生。等它真正"
                "完成后框架会另发一条完成播报，到那时再如实转达。"
            )
        return f"🚫 已转达拒绝：子任务已标记 failed。{msg}"

    register_approval_category("kanban_subtask", _on_resolve, ttl_seconds=7 * 86400)


def _schedule_artifact_ttl_cleanup() -> None:
    """注册每日 04:00 的 artifact TTL 清理 APScheduler job。

    幂等：``replace_existing=True``——多次启动 / 热重载只会保留最后一次。
    job 内部调 ``AIAgentArtifact.delete_expired()`` 走 ``@with_session`` 的事务，
    异常会被 APScheduler 吞掉并日志告警，不阻塞主循环。
    """
    from gsuid_core.aps import scheduler

    from .models import AIAgentArtifact

    async def _job() -> None:
        try:
            n = await AIAgentArtifact.delete_expired()
            if n > 0:
                logger.info(f"📋 [Kanban] 每日 TTL 清理删除 {n} 条过期 artifact")
        except Exception as e:
            logger.exception(f"📋 [Kanban] TTL 清理 job 执行失败: {e}")

    scheduler.add_job(
        func=_job,
        trigger="cron",
        hour=4,
        minute=0,
        id="kanban_artifact_ttl_cleanup",
        name="Kanban Artifact TTL 清理（每日 4:00）",
        replace_existing=True,
    )
    logger.info("📋 [Kanban] Artifact TTL 清理 job 已注册（每日 04:00）")


async def _recover_zombies_and_kick() -> None:
    """复活僵尸子任务后，对受影响的根任务各 kick 一次以重新进入调度。"""

    from gsuid_core.utils.database.base_models import async_maker

    from .kanban import recover_zombie_subtasks
    from .models import AIAgentTask
    from .kanban_executor import kick_root

    recovered = await recover_zombie_subtasks()
    # 无论是否复活过僵尸都无条件接力 kick 所有 running/pending 根任务（否则优雅重启后
    # 无僵尸时 pending 树永远无人推进）；双跑由 mark_subtask_running 的条件 SQL 拦住
    logger.info(f"📋 [Kanban] 启动期僵尸恢复 {recovered} 个，开始接力 kick 所有 running/pending 根任务")
    async with async_maker() as session:
        stmt = (
            select(AIAgentTask)
            .where(col(AIAgentTask.node_kind) == "root")
            .where(col(AIAgentTask.status).in_(("running", "pending")))
        )
        result = await session.execute(stmt)
        roots = [r for r in result.scalars().all() if r.id]
    import asyncio

    for r in roots:
        asyncio.create_task(kick_root(r.id))
