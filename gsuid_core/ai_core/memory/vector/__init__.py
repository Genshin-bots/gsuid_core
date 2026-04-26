"""记忆系统向量层模块

复用 rag/base.py 的 AsyncQdrantClient 和 TextEmbedding，
新增三个记忆专用 Qdrant Collection。
"""

from .ops import (
    search_edges,
    search_entities,
    search_episodes,
    upsert_edge_vector,
    upsert_entity_vector,
    upsert_episode_vector,
)
from .startup import ensure_memory_collections
from .collections import (
    MEMORY_EDGES_COLLECTION,
    MEMORY_ENTITIES_COLLECTION,
    MEMORY_EPISODES_COLLECTION,
)

__all__ = [
    "MEMORY_EPISODES_COLLECTION",
    "MEMORY_ENTITIES_COLLECTION",
    "MEMORY_EDGES_COLLECTION",
    "upsert_episode_vector",
    "upsert_entity_vector",
    "upsert_edge_vector",
    "search_episodes",
    "search_entities",
    "search_edges",
    "ensure_memory_collections",
]
