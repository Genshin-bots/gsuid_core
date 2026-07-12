"""RAG模块初始化"""

import asyncio
from typing import Callable, Awaitable

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

    # 1.2 Qdrant 后端(local/remote)发生切换时，把旧后端历史数据迁移到新后端(保留原数据)。
    # 必须在 init_embedding_model 之后(全局 client 已指向新后端)、各 Collection 初始化之前执行。
    from .qdrant_provider import migrate_qdrant_if_provider_changed

    await migrate_qdrant_if_provider_changed()

    # 1.5 启动阶段严格解析真实嵌入维度，避免未知维度时创建错误的 Qdrant Collection
    await ensure_embedding_dimension()

    # 2. 初始化工具、知识和图片集合。三个 init 均幂等；启动高负载窗口下 Qdrant
    # 偶发短暂不响应（ReadTimeout）会把 RAG 步骤判死，故对瞬时故障有限次重试。
    async def _init_with_retry(
        name: str,
        fn: Callable[[], Awaitable[None]],
        attempts: int = 3,
        delay: float = 8.0,
    ) -> None:
        for i in range(1, attempts + 1):
            try:
                await fn()
                return
            except Exception as e:
                if i == attempts:
                    raise
                logger.warning(f"🧠 [RAG] {name} 第{i}次失败({type(e).__name__})，{delay}s 后重试")
                await asyncio.sleep(delay)

    from . import init_image_collection, init_tools_collection, init_knowledge_collection

    await _init_with_retry("init_tools_collection", init_tools_collection)
    await _init_with_retry("init_knowledge_collection", init_knowledge_collection)
    await _init_with_retry("init_image_collection", init_image_collection)

    all_tools = get_all_tools()
    await sync_tools(all_tools)
    logger.info(f"🧠 [Tools] buildin_tools 已导入，当前 _TOOL_REGISTRY 大小: {len(all_tools)}")

    from . import sync_images, sync_knowledge

    await sync_knowledge()
    await sync_images()

    # 手动知识 SQL 真值源 ↔ Qdrant 向量对账：
    # ① 回填旧的"仅 Qdrant"手动知识到 SQL；② 向量库丢失/换模型后从 SQL 重嵌缺失分片。
    # 失败不影响启动（函数内自带兜底），数量一致时快速跳过。
    from gsuid_core.ai_core.rag.knowledge import reconcile_manual_knowledge

    await reconcile_manual_knowledge()

    # 把 docs/skills 下全部开发文档（references/*.md）挂载进知识库的保留来源 source="skill_doc"，
    # 供能力代理用混合检索（dense+BM25）查阅。幂等：内容未变化时跳过，不会每次启动重复嵌入。
    from gsuid_core.ai_core.rag.skills_kb import sync_skill_docs

    await sync_skill_docs()
