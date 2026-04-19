"""记忆系统初始化入口

在 ai_core/rag/startup.py 之后调用。
前置条件：rag/base.py 的 init_embedding_model() 必须已执行。
"""

import asyncio

from gsuid_core.logger import logger
from gsuid_core.server import on_core_start


@on_core_start(priority=5)
async def init_memory_system():
    """初始化记忆系统的所有组件。

    初始化顺序：
    1. 检查 RAG 是否已启用（前置条件）
    2. 确保 Qdrant Collection 存在
    3. 创建 SQLAlchemy 数据库表
    4. 启动 IngestionWorker 后台任务
    """
    from gsuid_core.ai_core.rag.base import client, init_embedding_model

    if client is None:
        init_embedding_model()
        from gsuid_core.ai_core.rag.base import client

        if client is None:
            logger.debug("🧠 [Memory] RAG 未启用，跳过记忆系统初始化")
            return

    logger.info("🧠 [Memory] 开始初始化记忆系统...")

    # 1. 确保 Qdrant Collection 存在
    try:
        from .vector.startup import ensure_memory_collections

        await ensure_memory_collections()
        logger.info("🧠 [Memory] Qdrant Collection 初始化完成")
    except Exception as e:
        logger.error(f"🧠 [Memory] Qdrant Collection 初始化失败: {e}")
        return

    # 3. 启动 IngestionWorker 后台任务
    try:
        from .ingestion.worker import IngestionWorker

        worker = IngestionWorker()
        asyncio.create_task(worker.start())
        logger.info("🧠 [Memory] IngestionWorker 后台任务已启动")
    except Exception as e:
        logger.error(f"🧠 [Memory] IngestionWorker 启动失败: {e}")
        return

    logger.info("🧠 [Memory] 记忆系统初始化完成")
