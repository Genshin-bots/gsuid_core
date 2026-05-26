"""记忆系统初始化入口

在 ai_core/rag/startup.py 之后调用。
前置条件：rag/base.py 的 init_embedding_model() 必须已执行。
"""

from typing import TYPE_CHECKING, Optional

from gsuid_core.logger import logger
from gsuid_core.server import on_core_shutdown
from gsuid_core.ai_core.configs.ai_config import ai_config

if TYPE_CHECKING:
    from .ingestion.worker import IngestionWorker

# 模块级引用，供 /api/chat_with_history 调用 flush_all()
_ingestion_worker: Optional["IngestionWorker"] = None


async def init_memory_system():
    """初始化记忆系统的所有组件。

    初始化顺序：
    1. 检查 RAG 是否已启用（前置条件）
    2. 确保 Qdrant Collection 存在
    3. 创建 SQLAlchemy 数据库表
    4. 启动 IngestionWorker 后台任务

    由 ai_core/startup.py 的 init_ai_core() 在 RAG 初始化之后顺序调用，
    不会并发执行，因此无需加锁保护 _ingestion_worker。
    """
    # 检查AI总开关
    if not ai_config.get_config("enable").data:
        logger.info("🧠 [Memory] AI总开关已关闭，跳过记忆系统初始化")
        return

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

    # 3. 启动 IngestionWorker 后台任务（在独立线程中运行，避免 LLM 调用阻塞主事件循环）
    global _ingestion_worker
    if _ingestion_worker is not None:
        logger.info("🧠 [Memory] IngestionWorker 已存在，跳过重复启动")
    else:
        try:
            from .ingestion.worker import IngestionWorker

            _ingestion_worker = IngestionWorker()
            _ingestion_worker.start_in_thread()
            logger.info("🧠 [Memory] IngestionWorker 后台任务已启动（独立线程）")
        except Exception as e:
            logger.error(f"🧠 [Memory] IngestionWorker 启动失败: {e}")
            return

    # 3.5 C9：启动多模态摄入 Worker（独立队列，异步转述高价值图片）
    try:
        from .ingestion.multimodal import start_multimodal_worker

        start_multimodal_worker()
    except Exception as e:
        logger.warning(f"🧠 [Memory] C9 多模态摄入 Worker 启动失败: {e}")

    # 4. C11：注册记忆生命周期维护定时任务（每周一次衰减 / 巩固 / 遗忘）
    try:
        from gsuid_core.aps import scheduler

        from .lifecycle.consolidation_worker import run_lifecycle_maintenance

        scheduler.add_job(
            func=run_lifecycle_maintenance,
            trigger="interval",
            weeks=1,
            id="ai_memory_lifecycle_maintenance",
            replace_existing=True,
        )
        logger.info("🧠 [Memory] C11 记忆生命周期维护任务已注册（每周一次）")
    except Exception as e:
        logger.warning(f"🧠 [Memory] C11 生命周期维护任务注册失败: {e}")

    logger.info("🧠 [Memory] 记忆系统初始化完成")


def get_ingestion_worker():
    """获取 IngestionWorker 实例（需在记忆系统初始化后调用才有效）"""
    if _ingestion_worker is None:
        logger.warning("🧠 [Memory] IngestionWorker 尚未初始化，请确认记忆系统已启动")
    return _ingestion_worker


@on_core_shutdown(priority=20)
async def shutdown_memory_system():
    """关闭记忆系统后台摄入线程。

    需要在主事件循环销毁和解释器关闭前显式停止独立事件循环，避免后台线程
    在默认执行器关闭后继续调度任务。
    """
    global _ingestion_worker
    if _ingestion_worker is None:
        return

    logger.info("🧠 [Memory] 正在关闭 IngestionWorker...")
    try:
        await _ingestion_worker.stop()
    except Exception as e:
        logger.error(f"🧠 [Memory] IngestionWorker 关闭失败: {e}", exc_info=True)
    finally:
        _ingestion_worker = None

    # C9：关闭多模态摄入 Worker
    try:
        from .ingestion.multimodal import stop_multimodal_worker

        await stop_multimodal_worker()
    except Exception as e:
        logger.error(f"🧠 [Memory] 多模态摄入 Worker 关闭失败: {e}")
