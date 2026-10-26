"""自主行动的启动和关闭。导入本模块才会登记关闭钩子。"""

from gsuid_core.server import on_core_shutdown


async def init_autonomy() -> None:
    from gsuid_core.ai_core.autonomy.tools import sync_persona_artifacts
    from gsuid_core.ai_core.persona.config import persona_config_manager
    from gsuid_core.ai_core.autonomy.scheduler import autonomy_scheduler

    await autonomy_scheduler.start()
    for persona_name in persona_config_manager.get_all_configs():
        await sync_persona_artifacts(persona_name)


@on_core_shutdown
async def stop_autonomy() -> None:
    from gsuid_core.ai_core.autonomy.scheduler import autonomy_scheduler

    await autonomy_scheduler.stop()
