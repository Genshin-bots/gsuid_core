"""RAG模块初始化"""

import asyncio

from gsuid_core.logger import logger
from gsuid_core.ai_core.register import get_all_tools
from gsuid_core.ai_core.rag.tools import sync_tools
from gsuid_core.ai_core.configs.ai_config import ai_config

from .base import pre_download_models, init_embedding_model


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

    # 2. 初始化工具、知识和图片集合
    from . import init_image_collection, init_tools_collection, init_knowledge_collection

    await init_tools_collection()
    await init_knowledge_collection()
    await init_image_collection()

    all_tools = get_all_tools()
    await sync_tools(all_tools)
    logger.info(f"🧠 [Tools] buildin_tools 已导入，当前 _TOOL_REGISTRY 大小: {len(all_tools)}")

    # 3. 初始化System Prompt集合
    from gsuid_core.ai_core.system_prompt import (
        get_all_prompts,
        init_default_prompts,
        sync_to_vector_store,
        init_system_prompt_collection,
    )

    # 初始化默认System Prompt（如果还没有的话）
    added = init_default_prompts()
    logger.info(f"🧠 [SystemPrompt] 初始化了 {added} 个默认System Prompt")

    await init_system_prompt_collection()
    all_prompts = get_all_prompts()
    if all_prompts:
        await sync_to_vector_store(all_prompts)  # type: ignore

    from . import sync_images, sync_knowledge

    await sync_knowledge()
    await sync_images()
