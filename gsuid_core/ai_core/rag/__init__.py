"""RAG 导出面。子模块按名字再导入，避免 ``import qmodels`` 把知识库拉起来。

AI 关闭时，知识库 / 工具向量 / 图片库 / 重排 / 启动模块给出空壳，只打一条警告。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

from gsuid_core.ai_core.rag.shell import (
    SHELL_MODULES,
    ai_enabled,
    shell_attr,
    warn_rag_shell,
    install_rag_shell_finder,
)

if TYPE_CHECKING:
    from gsuid_core.ai_core.rag.base import (
        DB_PATH,
        MODELS_CACHE,
        DEFAULT_DIMENSION,
        RERANK_MODELS_CACHE,
        RERANKER_MODEL_NAME,
        EMBEDDING_MODEL_NAME,
        IMAGE_COLLECTION_NAME,
        TOOLS_COLLECTION_NAME,
        KNOWLEDGE_COLLECTION_NAME,
        client,
        get_point_id,
        is_enable_ai,
        get_dimension,
        calculate_hash,
        embedding_model,
        is_enable_rerank,
        embedding_provider,
        get_strict_dimension,
        init_embedding_model,
        ensure_embedding_dimension,
    )
    from gsuid_core.ai_core.rag.tools import sync_tools, search_tools, init_tools_collection
    from gsuid_core.ai_core.rag.startup import init_all
    from gsuid_core.ai_core.rag.reranker import get_reranker, rerank_results, reset_reranker
    from gsuid_core.ai_core.rag.embedding import (
        EmbeddingProvider,
        get_embedding_provider,
        reset_embedding_provider,
    )
    from gsuid_core.ai_core.rag.image_rag import (
        sync_images,
        search_images,
        get_image_list,
        build_image_text,
        delete_image_from_db,
        load_image_from_path,
        init_image_collection,
        search_and_load_image,
        get_image_path_by_query,
    )
    from gsuid_core.ai_core.rag.knowledge import (
        sync_knowledge,
        query_knowledge,
        build_knowledge_text,
        init_knowledge_collection,
    )

__all__ = [
    "DEFAULT_DIMENSION",
    "EMBEDDING_MODEL_NAME",
    "MODELS_CACHE",
    "DB_PATH",
    "RERANK_MODELS_CACHE",
    "RERANKER_MODEL_NAME",
    "TOOLS_COLLECTION_NAME",
    "KNOWLEDGE_COLLECTION_NAME",
    "IMAGE_COLLECTION_NAME",
    "is_enable_ai",
    "is_enable_rerank",
    "get_dimension",
    "get_strict_dimension",
    "ensure_embedding_dimension",
    "embedding_model",
    "embedding_provider",
    "client",
    "init_embedding_model",
    "get_point_id",
    "calculate_hash",
    "EmbeddingProvider",
    "get_embedding_provider",
    "reset_embedding_provider",
    "init_tools_collection",
    "sync_tools",
    "search_tools",
    "init_knowledge_collection",
    "sync_knowledge",
    "query_knowledge",
    "build_knowledge_text",
    "init_image_collection",
    "sync_images",
    "search_images",
    "get_image_list",
    "build_image_text",
    "delete_image_from_db",
    "load_image_from_path",
    "search_and_load_image",
    "get_image_path_by_query",
    "get_reranker",
    "reset_reranker",
    "rerank_results",
    "init_all",
]

_BASE = {
    "DEFAULT_DIMENSION",
    "EMBEDDING_MODEL_NAME",
    "MODELS_CACHE",
    "DB_PATH",
    "RERANK_MODELS_CACHE",
    "RERANKER_MODEL_NAME",
    "TOOLS_COLLECTION_NAME",
    "KNOWLEDGE_COLLECTION_NAME",
    "IMAGE_COLLECTION_NAME",
    "is_enable_ai",
    "is_enable_rerank",
    "get_dimension",
    "get_strict_dimension",
    "ensure_embedding_dimension",
    "embedding_model",
    "embedding_provider",
    "client",
    "init_embedding_model",
    "get_point_id",
    "calculate_hash",
}
_EMBEDDING = {
    "EmbeddingProvider",
    "get_embedding_provider",
    "reset_embedding_provider",
}
_TOOLS = {"init_tools_collection", "sync_tools", "search_tools"}
_KNOWLEDGE = {
    "init_knowledge_collection",
    "sync_knowledge",
    "query_knowledge",
    "build_knowledge_text",
}
_IMAGE = {
    "init_image_collection",
    "sync_images",
    "search_images",
    "get_image_list",
    "build_image_text",
    "delete_image_from_db",
    "load_image_from_path",
    "search_and_load_image",
    "get_image_path_by_query",
}
_RERANK = {"get_reranker", "reset_reranker", "rerank_results"}


def __getattr__(name: str) -> object:
    if name in _BASE:
        module_name = "gsuid_core.ai_core.rag.base"
    elif name in _EMBEDDING:
        module_name = "gsuid_core.ai_core.rag.embedding"
    elif name in _TOOLS:
        module_name = "gsuid_core.ai_core.rag.tools"
    elif name in _KNOWLEDGE:
        module_name = "gsuid_core.ai_core.rag.knowledge"
    elif name in _IMAGE:
        module_name = "gsuid_core.ai_core.rag.image_rag"
    elif name in _RERANK:
        module_name = "gsuid_core.ai_core.rag.reranker"
    elif name == "init_all":
        module_name = "gsuid_core.ai_core.rag.startup"
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if module_name in SHELL_MODULES and not ai_enabled():
        warn_rag_shell()
        value = shell_attr(name)
        globals()[name] = value
        return value
    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value


if not ai_enabled():
    install_rag_shell_finder()
