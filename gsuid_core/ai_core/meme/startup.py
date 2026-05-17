"""表情包模块启动初始化

由 ai_core/startup.py 的 init_ai_core() 统一调用：
1. 确保目录存在
2. 确保 Qdrant Collection 存在
3. 启动后台打标 worker
"""

from gsuid_core.logger import logger
from gsuid_core.server import on_core_shutdown
from gsuid_core.ai_core.meme.library import get_memes_base_path


async def init_meme_module():
    """初始化表情包模块"""
    from gsuid_core.ai_core.meme.config import meme_config
    from gsuid_core.ai_core.configs.ai_config import ai_config

    enable_ai: bool = ai_config.get_config("enable").data
    if not enable_ai:
        return

    if not meme_config.get_config("meme_enable").data:
        logger.info("[Meme] 表情包模块未启用，跳过初始化")
        return

    logger.info("[Meme] 开始初始化表情包模块...")

    # 1. 确保目录存在
    base = get_memes_base_path()
    for folder in ["inbox", "common", "rejected"]:
        (base / folder).mkdir(parents=True, exist_ok=True)
    logger.info("[Meme] 目录结构初始化完成")

    # 2. 确保 Qdrant Collection 存在
    try:
        from gsuid_core.ai_core.meme.library import _ensure_meme_collection

        await _ensure_meme_collection()
        logger.info("[Meme] Qdrant Collection 初始化完成")
    except Exception as e:
        logger.warning(f"[Meme] Qdrant Collection 初始化失败（非致命）: {e}")

    # 3. 启动后台打标 worker
    try:
        from gsuid_core.ai_core.meme.tagger import start_tag_worker

        await start_tag_worker()
        logger.info("[Meme] 打标 worker 启动完成")
    except Exception as e:
        logger.error(f"[Meme] 打标 worker 启动失败: {e}")

    # 4. 处理 inbox 中遗留的待打标图片
    try:
        from gsuid_core.ai_core.meme.tagger import enqueue_tag
        from gsuid_core.ai_core.meme.database_model import AiMemeRecord

        pending_records = await AiMemeRecord.get_pending_records(limit=50)
        for record in pending_records:
            await enqueue_tag(record.meme_id)
        if pending_records:
            logger.info(f"[Meme] 已将 {len(pending_records)} 条遗留记录加入打标队列")
    except Exception as e:
        logger.warning(f"[Meme] 处理遗留记录失败: {e}")

    logger.info("[Meme] 表情包模块初始化完成")


@on_core_shutdown
async def shutdown_meme_module():
    """关闭表情包模块"""
    try:
        from gsuid_core.ai_core.meme.tagger import stop_tag_worker

        await stop_tag_worker()
    except Exception:
        pass
