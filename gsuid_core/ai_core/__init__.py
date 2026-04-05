from gsuid_core.server import on_core_start
from gsuid_core.ai_core.rag import init_all
from gsuid_core.ai_core.ai_router import SessionManager


@on_core_start
async def init_ai_core():
    """初始化AI Core的RAG、Session管理器和定时巡检"""
    await init_all()
    await SessionManager.start_cleanup_loop()

    # 启动定时巡检（如果启用了该模式）
    from gsuid_core.ai_core.heartbeat import start_heartbeat_inspector

    await start_heartbeat_inspector()
