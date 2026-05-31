"""RAG模块初始化"""

import asyncio

from gsuid_core.logger import logger
from gsuid_core.ai_core.register import get_all_tools
from gsuid_core.ai_core.rag.tools import sync_tools
from gsuid_core.ai_core.configs.ai_config import ai_config

from .base import pre_download_models, init_embedding_model, ensure_embedding_dimension


async def init_all():
    """初始化RAG模块的所有组件"""
    # 检查AI总开关
    if not ai_config.get_config("enable").data:
        logger.info("🧠 [RAG] AI总开关已关闭，跳过RAG模块初始化")
        return

    # 0. 提前下载所有模型到缓存目录
    await pre_download_models()

    # 1. 初始化Embedding模型和Qdrant客户端
    # 模型加载是同步 CPU 密集操作，放到线程执行避免冻住事件循环
    await asyncio.to_thread(init_embedding_model)

    # 1.5 启动阶段严格解析真实嵌入维度，避免未知维度时创建错误的 Qdrant Collection
    await ensure_embedding_dimension()

    # 2. 初始化工具、知识和图片集合
    from . import init_image_collection, init_tools_collection, init_knowledge_collection

    await init_tools_collection()
    await init_knowledge_collection()
    await init_image_collection()

    all_tools = get_all_tools()
    await sync_tools(all_tools)
    logger.info(f"🧠 [Tools] buildin_tools 已导入，当前 _TOOL_REGISTRY 大小: {len(all_tools)}")

    from . import sync_images, sync_knowledge

    await sync_knowledge()
    await sync_images()
