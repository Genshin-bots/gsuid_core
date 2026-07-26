"""表情包模块启动初始化

由 ai_core/startup.py 的 init_ai_core() 统一调用：
1. 确保目录存在
2. 确保 Qdrant Collection 存在
3. 启动后台打标 worker
"""

from gsuid_core.i18n import t
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
        logger.info(t("log.meme.module_enabled_skipping"))
        return

    logger.info(t("log.meme.module_initialization"))

    # 1. 确保目录存在
    base = get_memes_base_path()
    for folder in ["inbox", "common", "rejected"]:
        (base / folder).mkdir(parents=True, exist_ok=True)
    logger.info(t("log.meme.directory_structure_initialization"))

    # 2. 确保 Qdrant Collection 存在
    try:
        from gsuid_core.ai_core.meme.library import _ensure_meme_collection

        await _ensure_meme_collection()
        logger.info(t("log.meme.qdrant_collection_initialization"))
    except Exception as e:
        logger.warning(t("log.meme.qdrant_collection_initialization_non", e=e))

    # 3. 启动后台打标 worker
    try:
        from gsuid_core.ai_core.meme.tagger import start_tag_worker

        await start_tag_worker()
        logger.info(t("log.meme.tagging_worker_startup"))
    except Exception as e:
        logger.error(t("log.meme.tagging_worker_startup_2", e=e))

    # 4. 处理 inbox 中遗留的待打标图片（仅在 VLM 打标启用时；关闭时由 worker/scanner
    #    在网页控制台开启 meme_vlm_enable 后自动补回 pending 记录）
    if meme_config.get_config("meme_vlm_enable").data:
        try:
            from gsuid_core.ai_core.meme.tagger import enqueue_tag
            from gsuid_core.ai_core.meme.database_model import AiMemeRecord

            pending_records = await AiMemeRecord.get_pending_records(limit=50)
            for record in pending_records:
                await enqueue_tag(record.meme_id)
            if pending_records:
                logger.info(t("log.meme.added_legacy_records_tagging", p0=len(pending_records)))
        except Exception as e:
            logger.warning(t("log.meme.process_legacy_record", e=e))
    else:
        logger.info(t("log.meme.vlm_tagging_enabled"))

    logger.info(t("log.meme.module_initialization_2"))


@on_core_shutdown
async def shutdown_meme_module():
    """关闭表情包模块"""
    try:
        from gsuid_core.ai_core.meme.tagger import stop_tag_worker

        await stop_tag_worker()
    except Exception:
        pass
