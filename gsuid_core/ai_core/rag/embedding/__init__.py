"""嵌入模型提供方抽象层

提供统一的嵌入接口，支持本地模型（fastembed）和 OpenAI 兼容格式的远程 API。
通过 ai_config 中的 embedding_provider 配置项切换底层实现。

本包由原 ``embedding.py`` 拆分而来，保留 ``gsuid_core.ai_core.rag.embedding``
作为统一的导入出口，子模块划分如下：

- modality: 模态枚举 EmbeddingModality 与 parse_modalities 解析工具
- base: 抽象基类 EmbeddingProvider 及线程池桥接
- local: 本地 fastembed 实现 LocalEmbeddingProvider
- openai: OpenAI 兼容远程实现 OpenAIEmbeddingProvider
- factory: 全局单例管理 get_embedding_provider / reset_embedding_provider

使用方式:
    from gsuid_core.ai_core.rag.embedding import get_embedding_provider

    provider = get_embedding_provider()
    vectors = provider.embed(["hello", "world"])       # 批量
    vector = provider.embed_single("hello")             # 单条
    dim = provider.dimension                            # 向量维度
"""

from gsuid_core.ai_core.rag.embedding.base import EmbeddingProvider
from gsuid_core.ai_core.rag.embedding.local import LocalEmbeddingProvider
from gsuid_core.ai_core.rag.embedding.openai import OpenAIEmbeddingProvider
from gsuid_core.ai_core.rag.embedding.factory import (
    get_embedding_provider,
    reset_embedding_provider,
)
from gsuid_core.ai_core.rag.embedding.modality import (
    EmbeddingModality,
    parse_modalities,
)

__all__ = [
    "EmbeddingModality",
    "parse_modalities",
    "EmbeddingProvider",
    "LocalEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "get_embedding_provider",
    "reset_embedding_provider",
]
