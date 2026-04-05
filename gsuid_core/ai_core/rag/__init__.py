"""RAG模块 - 向量检索与知识库管理

提供基于向量数据库的RAG（检索增强生成）功能，
包括工具检索、知识库查询和结果重排序。

主要组件:
- base: 共享常量和工具函数、全局变量
- tools: 工具向量存储
- knowledge: 知识库同步与查询
- reranker: 结果重排序
- init: 模块初始化
"""

from gsuid_core.ai_core.rag.base import (
    DB_PATH,
    # 常量
    DIMENSION,
    MODELS_CACHE,
    RERANK_MODELS_CACHE,
    RERANKER_MODEL_NAME,
    EMBEDDING_MODEL_NAME,
    TOOLS_COLLECTION_NAME,
    KNOWLEDGE_COLLECTION_NAME,
    client,
    get_point_id,
    # 配置（动态函数）
    is_enable_ai,
    calculate_hash,
    # 全局变量
    embedding_model,
    is_enable_rerank,
    # 函数
    init_embedding_model,
)
from gsuid_core.ai_core.rag.tools import (
    sync_tools,
    search_tools,
    init_tools_collection,
)
from gsuid_core.ai_core.rag.startup import init_all
from gsuid_core.ai_core.rag.reranker import (
    get_reranker,
    rerank_results,
)
from gsuid_core.ai_core.rag.knowledge import (
    sync_knowledge,
    query_knowledge,
    build_knowledge_text,
    init_knowledge_collection,
)

__all__ = [
    # base - 常量
    "DIMENSION",
    "EMBEDDING_MODEL_NAME",
    "MODELS_CACHE",
    "DB_PATH",
    "RERANK_MODELS_CACHE",
    "RERANKER_MODEL_NAME",
    "TOOLS_COLLECTION_NAME",
    "KNOWLEDGE_COLLECTION_NAME",
    # base - 配置（动态函数）
    "is_enable_ai",
    "is_enable_rerank",
    # base - 全局变量
    "embedding_model",
    "client",
    # base - 函数
    "init_embedding_model",
    "get_point_id",
    "calculate_hash",
    # tools
    "init_tools_collection",
    "sync_tools",
    "search_tools",
    # knowledge
    "init_knowledge_collection",
    "sync_knowledge",
    "query_knowledge",
    "build_knowledge_text",
    # reranker
    "get_reranker",
    "rerank_results",
    # init
    "init_all",
]
