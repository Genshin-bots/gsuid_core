"""RAG模块初始化"""

from gsuid_core.server import on_core_start

from .base import init_embedding_model


@on_core_start
async def init_all():
    """初始化RAG模块的所有组件"""
    # 1. 初始化Embedding模型和Qdrant客户端
    init_embedding_model()

    # 2. 初始化工具和知识集合
    from . import init_tools_collection, init_knowledge_collection

    await init_tools_collection()
    await init_knowledge_collection()

    from gsuid_core.logger import logger
    from gsuid_core.ai_core.register import _TOOL_REGISTRY

    logger.info(f"🧠 [Tools] buildin_tools 已导入，当前 _TOOL_REGISTRY 大小: {len(_TOOL_REGISTRY)}")

    from . import sync_tools, sync_knowledge

    await sync_tools(_TOOL_REGISTRY)
    await sync_knowledge()
